"""Payment gateway callbacks, sandbox gateway, SMS webhook and invoice downloads."""
import secrets

from flask import Blueprint, Response, abort, jsonify, redirect, render_template, request, url_for

from .. import csrf_exempt, db
from ..models import Invoice, Payment
from ..services import payments as pay
from ..services.core import audit, get_setting
from ..services.pdf import build_pdf
from ..utils import fmt_date

bp = Blueprint("pay", __name__)


def invoice_pdf_response(inv):
    c = inv.customer
    lines = [
        (get_setting("company_name"), 20, True),
        (get_setting("company_address") or "", 10, False),
        ("", 10, False),
        (f"INVOICE {inv.number}", 16, True),
        (f"Date: {fmt_date(inv.created_at)}    Status: {inv.status.upper()}", 11, False),
        (f"Due: {fmt_date(inv.due_date)}", 11, False),
        ("", 10, False),
        ("Bill to", 12, True),
        (f"{c.name}  ({c.username})", 11, False),
        (f"Phone: {c.phone}", 11, False),
        (c.address or "", 11, False),
        ("", 10, False),
        ("Description", 12, True),
        (f"{inv.description or inv.kind}   -   {inv.amount:,.2f} BDT", 11, False),
        ("", 10, False),
        (f"TOTAL: {inv.amount:,.2f} BDT", 14, True),
        (f"Paid on: {fmt_date(inv.paid_at)}" if inv.paid_at else "Payment pending", 11, False),
    ]
    return Response(build_pdf(lines), mimetype="application/pdf",
                    headers={"Content-Disposition": f"attachment; filename={inv.number}.pdf"})


# ------------------------------------------------------------- sandbox gateway
@bp.route("/pay/sandbox/<int:pid>", methods=["GET", "POST"])
def sandbox(pid):
    if get_setting("sandbox_enabled") != "1":
        abort(404)
    p = db.get_or_404(Payment, pid)
    if p.method != "sandbox" or p.status != "pending":
        abort(404)
    if request.method == "POST":
        if request.form.get("result") == "pay":
            pay.finalize(p, "SBX" + secrets.token_hex(4).upper())
        else:
            p.status = "failed"
        db.session.commit()
        return redirect("/portal/history")
    return render_template("pay_sandbox.html", p=p)


# ------------------------------------------------------------- real gateway callbacks
@bp.route("/pay/callback/bkash/<int:pid>")
def cb_bkash(pid):
    p = db.get_or_404(Payment, pid)
    try:
        pay.complete_bkash(p, request.args.get("paymentID", p.gateway_ref), request.args.get("status", ""))
    except pay.PaymentError:
        p.status = "failed"
    db.session.commit()
    return redirect("/portal/history")


@bp.route("/pay/callback/sslcommerz/<int:pid>", methods=["GET", "POST"])
@csrf_exempt
def cb_ssl(pid):
    p = db.get_or_404(Payment, pid)
    try:
        if p.status != "success":
            pay.complete_sslcommerz(p, request.values.get("val_id", ""))
    except pay.PaymentError:
        p.status = "failed"
    db.session.commit()
    return redirect("/portal/history")


@bp.route("/pay/fail/<int:pid>", methods=["GET", "POST"])
@csrf_exempt
def cb_fail(pid):
    p = db.get_or_404(Payment, pid)
    if p.status == "pending":
        p.status = "failed"
        db.session.commit()
    return redirect("/portal/history")


# ------------------------------------------------------------- SMS webhook (auto send-money)
@bp.route("/api/sms-webhook", methods=["POST"])
@csrf_exempt
def sms_webhook():
    """Point an SMS-forwarder app on the merchant phone here.
    Header `X-Token: <sms_webhook_token>`; JSON {"message": "..."} or form field `message`/`text`."""
    token = get_setting("sms_webhook_token")
    sent = request.headers.get("X-Token") or request.values.get("token", "")
    if not token or not secrets.compare_digest(token, sent):
        audit("sms_webhook_denied", "bad token", level="security")
        db.session.commit()
        abort(401)
    data = request.get_json(silent=True) or request.values
    text = data.get("message") or data.get("text") or data.get("body") or ""
    rt, p = pay.ingest_sms(text, (data.get("provider") or "").lower()[:20])
    db.session.commit()
    return jsonify(ok=bool(rt), activated=bool(p and p.status == "success"))
