"""Payment gateways: bKash (tokenized checkout), SSLCommerz, built-in sandbox and
manual Send-Money (Nagad / Rocket / Upay / bKash) with SMS-based auto verification."""
import json
import re
import urllib.parse
import urllib.request

from flask import current_app

from .. import db
from ..models import Payment, ReceivedTxn
from ..utils import money, utcnow
from .billing import ledger, record_payment, settle_invoice
from .core import audit, get_setting

SEND_MONEY = {"bkash": "bKash", "nagad": "Nagad", "rocket": "Rocket", "upay": "Upay"}


class PaymentError(Exception):
    pass


def available_methods():
    m = []
    if get_setting("bkash_enabled") == "1":
        m.append(("bkash_api", "bKash (Online)", "redirect"))
    if get_setting("sslcommerz_enabled") == "1":
        m.append(("sslcommerz", "Card / Mobile Banking (SSLCommerz)", "redirect"))
    if get_setting("sandbox_enabled") == "1":
        m.append(("sandbox", "Test Gateway (sandbox)", "redirect"))
    if get_setting("manual_enabled") == "1":
        for k, label in SEND_MONEY.items():
            if get_setting(f"{k}_number"):
                m.append((f"manual_{k}", f"{label} Send Money", "manual"))
    return m


def _http(url, data=None, headers=None, method=None, form=False):
    body = None
    h = dict(headers or {})
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            h.setdefault("Content-Type", "application/x-www-form-urlencoded")
        else:
            body = json.dumps(data).encode()
            h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=h, method=method or ("POST" if body else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8", "replace") or "{}")
    except Exception as e:
        raise PaymentError(f"gateway error: {e}")


def _cb(path):
    return current_app.config["BASE_URL"].rstrip("/") + path


# -------------------------------------------------------------------- start
def start(customer, invoice, method):
    """Create a pending Payment. Returns ('redirect', url, payment) or ('manual', None, payment)."""
    p = Payment(customer_id=customer.id, invoice_id=invoice.id, method="pending", amount=invoice.amount,
                net_amount=invoice.amount, status="pending")
    db.session.add(p)
    db.session.flush()
    if method == "sandbox":
        p.method = "sandbox"
        return "redirect", f"/pay/sandbox/{p.id}", p
    if method == "bkash_api":
        p.method = "bkash"
        tok = _bkash_token()
        r = _http(get_setting("bkash_base") + "/tokenized/checkout/create", {
            "mode": "0011", "payerReference": customer.username, "callbackURL": _cb(f"/pay/callback/bkash/{p.id}"),
            "amount": f"{invoice.amount:.2f}", "currency": "BDT", "intent": "sale",
            "merchantInvoiceNumber": invoice.number}, _bkash_headers(tok))
        if not r.get("bkashURL"):
            raise PaymentError(r.get("statusMessage", "bKash create failed"))
        p.gateway_ref = r.get("paymentID", "")
        return "redirect", r["bkashURL"], p
    if method == "sslcommerz":
        p.method = "sslcommerz"
        p.gateway_ref = f"ISP{p.id}-{int(utcnow().timestamp())}"
        host = "sandbox.sslcommerz.com" if get_setting("sslcommerz_sandbox") == "1" else "securepay.sslcommerz.com"
        r = _http(f"https://{host}/gwprocess/v4/api.php", {
            "store_id": get_setting("sslcommerz_store_id"), "store_passwd": get_setting("sslcommerz_store_pass"),
            "total_amount": f"{invoice.amount:.2f}", "currency": "BDT", "tran_id": p.gateway_ref,
            "success_url": _cb(f"/pay/callback/sslcommerz/{p.id}"), "fail_url": _cb(f"/pay/fail/{p.id}"),
            "cancel_url": _cb(f"/pay/fail/{p.id}"), "ipn_url": _cb(f"/pay/callback/sslcommerz/{p.id}"),
            "cus_name": customer.name, "cus_email": customer.email or "na@example.com",
            "cus_add1": customer.address or "Dhaka", "cus_city": "Dhaka", "cus_country": "Bangladesh",
            "cus_phone": customer.phone or "01700000000", "shipping_method": "NO",
            "product_name": invoice.description or "Internet", "product_category": "service",
            "product_profile": "non-physical-goods"}, form=True)
        if not r.get("GatewayPageURL"):
            raise PaymentError(r.get("failedreason", "SSLCommerz init failed"))
        return "redirect", r["GatewayPageURL"], p
    if method.startswith("manual_"):
        p.method = method.split("_", 1)[1]
        p.status = "pending"
        return "manual", None, p
    raise PaymentError("Unknown payment method")


def _bkash_headers(token=None):
    h = {"Accept": "application/json", "X-APP-Key": get_setting("bkash_app_key")}
    if token:
        h["Authorization"] = token
    else:
        h.update(username=get_setting("bkash_username"), password=get_setting("bkash_password"))
    return h


def _bkash_token():
    r = _http(get_setting("bkash_base") + "/tokenized/checkout/token/grant",
              {"app_key": get_setting("bkash_app_key"), "app_secret": get_setting("bkash_app_secret")},
              _bkash_headers())
    if not r.get("id_token"):
        raise PaymentError(r.get("statusMessage", "bKash auth failed"))
    return r["id_token"]


# -------------------------------------------------------------------- completion
def finalize(payment, trx_id=""):
    """Money has really arrived for `payment`: settle invoice, credit reseller commission."""
    if payment.status == "success":
        return payment
    inv = payment.invoice
    c = payment.customer
    payment.trx_id = trx_id or payment.trx_id
    if c.reseller and not c.reseller.is_admin and payment.method not in ("reseller", "cash") and inv.kind == "package":
        cost = c.reseller.cost_for(inv.amount)
        # online money lands with the ISP; the reseller earns their margin
        ledger(c.reseller, "commission", money(inv.amount - cost), f"Online sale {c.username}", c)
        payment.reseller_id = c.reseller.id
        payment.net_amount = cost
    settle_invoice(inv, payment)
    return payment


def complete_bkash(payment, payment_id, status):
    if status != "success":
        payment.status = "failed"
        return False
    r = _http(get_setting("bkash_base") + "/tokenized/checkout/execute", {"paymentID": payment_id},
              _bkash_headers(_bkash_token()))
    if r.get("transactionStatus") == "Completed" and money(r.get("amount")) == money(payment.amount):
        finalize(payment, r.get("trxID", ""))
        return True
    payment.status, payment.note = "failed", str(r.get("statusMessage", ""))[:200]
    return False


def complete_sslcommerz(payment, val_id):
    host = "sandbox.sslcommerz.com" if get_setting("sslcommerz_sandbox") == "1" else "securepay.sslcommerz.com"
    q = urllib.parse.urlencode({"val_id": val_id, "store_id": get_setting("sslcommerz_store_id"),
                                "store_passwd": get_setting("sslcommerz_store_pass"), "format": "json"})
    r = _http(f"https://{host}/validator/api/validationserverAPI.php?{q}")
    if r.get("status") in ("VALID", "VALIDATED") and r.get("tran_id") == payment.gateway_ref \
            and money(r.get("amount")) >= money(payment.amount):
        finalize(payment, r.get("bank_tran_id", val_id))
        return True
    payment.status = "failed"
    return False


# -------------------------------------------------------------------- manual send-money
_TRX = re.compile(r"(?:TrxID|TxnID|Txn ID|Trx ID|TransactionID|Transaction ID)\s*[:\-]?\s*([A-Za-z0-9]{6,})", re.I)
_AMT = re.compile(r"(?:Tk|BDT|৳)\.?\s*([\d,]+(?:\.\d+)?)", re.I)
_SENDER = re.compile(r"(?:from|sender|A/C)\s*:?\s*(?:A/C\s*:?\s*)?((?:\+?880|0)1\d{9})", re.I)


def parse_sms(text):
    t = text or ""
    low = t.lower()
    provider = next((p for p in ("bkash", "nagad", "rocket", "upay") if p in low), "")
    trx, amt, snd = _TRX.search(t), _AMT.search(t), _SENDER.search(t)
    if not (trx and amt):
        return None
    return {"provider": provider, "trx_id": trx.group(1).upper(), "amount": money(amt.group(1).replace(",", "")),
            "sender": snd.group(1) if snd else "", "raw": t[:1000]}


def ingest_sms(text, provider_hint=""):
    """Handle an SMS forwarded from the merchant phone. Returns (ReceivedTxn|None, payment|None)."""
    d = parse_sms(text)
    if not d:
        return None, None
    if db.session.query(ReceivedTxn).filter_by(trx_id=d["trx_id"]).first():
        return None, None
    rt = ReceivedTxn(provider=d["provider"] or provider_hint, trx_id=d["trx_id"], sender=d["sender"],
                     amount=d["amount"], raw=d["raw"])
    db.session.add(rt)
    db.session.flush()
    pend = Payment.query.filter(Payment.trx_id == rt.trx_id, Payment.status.in_(["review", "pending"])).first()
    if pend:
        _claim(rt, pend)
    return rt, pend


def _claim(rt, payment):
    if rt.amount + 0.001 >= payment.amount and rt.claimed_by is None:
        rt.claimed_by = payment.id
        finalize(payment, rt.trx_id)
        audit("sendmoney_auto", f"{payment.customer.username} trx={rt.trx_id} amount={rt.amount}")
        return True
    payment.status, payment.note = "review", f"Amount mismatch: received {rt.amount}, expected {payment.amount}"
    return False


def submit_manual(payment, trx_id, sender=""):
    """Customer entered the TrxID of the money they sent."""
    trx = (trx_id or "").strip().upper()
    if len(trx) < 6:
        raise PaymentError("Invalid transaction ID")
    if Payment.query.filter(Payment.trx_id == trx, Payment.status == "success").first():
        raise PaymentError("This transaction ID has already been used")
    payment.trx_id, payment.sender = trx, sender
    payment.status = "review"
    rt = ReceivedTxn.query.filter_by(trx_id=trx).first()
    if rt:
        if rt.claimed_by:
            raise PaymentError("This transaction ID has already been used")
        _claim(rt, payment)
    else:
        payment.note = "Waiting for payment confirmation"
    return payment


def approve_manually(payment, staff):
    if payment.status == "success":
        return
    finalize(payment, payment.trx_id)
    audit("payment_manual_approve", f"{payment.id} by {staff.username}", actor=staff.username)


def reject(payment, staff):
    payment.status = "failed"
    audit("payment_reject", f"{payment.id} by {staff.username}", actor=staff.username)
