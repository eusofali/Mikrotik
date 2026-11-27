"""Invoices, payments, package activation and the reseller/distributor wallet."""
from datetime import timedelta

from .. import db
from ..models import Customer, Invoice, LedgerEntry, Payment, Staff
from ..utils import money, utcnow
from .core import audit, get_int
from .provision import sync_customer
from .sms import notify


class BillingError(Exception):
    pass


# ----------------------------------------------------------------- invoices
def create_invoice(customer, amount, kind="package", package=None, description="", period="", due_days=None):
    due_days = get_int("invoice_due_days", 7) if due_days is None else due_days
    inv = Invoice(customer_id=customer.id, package_id=package.id if package else None, kind=kind,
                  amount=money(amount), description=description or (package.name if package else ""),
                  period=period, due_date=utcnow() + timedelta(days=due_days))
    db.session.add(inv)
    db.session.flush()
    inv.number = f"INV-{utcnow():%Y%m%d}-{inv.id:05d}"
    return inv


# ----------------------------------------------------------------- activation
def activate_package(customer, package, notify_sms=True):
    """Start/extend a prepaid package. Stacks on top of remaining validity."""
    now = utcnow()
    base = customer.expires_at if customer.expires_at and customer.expires_at > now and \
        customer.package_id == package.id else now
    customer.package_id = package.id
    customer.kind = package.kind
    customer.expires_at = base + timedelta(days=package.validity_days)
    customer.status = "active"
    customer.expiry_notified = None
    sync_customer(customer)
    if notify_sms:
        notify(customer, "activation")


def reactivate_postpaid(customer):
    if customer.status == "suspended" and customer.due_amount <= 0:
        customer.status = "active"
        sync_customer(customer)


def settle_invoice(invoice, payment):
    """Mark an invoice paid by `payment` and apply its effect on the customer's service."""
    if invoice.status == "paid":
        return
    c = invoice.customer
    invoice.status, invoice.paid_at = "paid", utcnow()
    payment.invoice_id = invoice.id
    payment.status, payment.paid_at = "success", utcnow()
    if invoice.kind == "package" and invoice.package:
        activate_package(c, invoice.package, notify_sms=False)
    elif invoice.kind == "monthly":
        reactivate_postpaid(c)
    notify(c, "payment", amount=f"{payment.amount:g}", trx=payment.trx_id or payment.method.upper(),
           invoice=invoice.number)
    audit("payment_success", f"{c.username} {payment.method} {payment.amount} inv={invoice.number}",
          actor=c.username)


def record_payment(customer, invoice, method, amount, trx_id="", sender="", status="success", gateway_ref="",
                   reseller=None, net=None, note=""):
    p = Payment(customer_id=customer.id, invoice_id=invoice.id if invoice else None, method=method,
                amount=money(amount), net_amount=money(amount if net is None else net), trx_id=trx_id,
                sender=sender, status="pending", gateway_ref=gateway_ref,
                reseller_id=reseller.id if reseller else None, note=note)
    db.session.add(p)
    db.session.flush()
    if status == "success" and invoice:
        settle_invoice(invoice, p)
    else:
        p.status = status
    return p


# ----------------------------------------------------------------- wallet
def ledger(staff, kind, amount, note="", customer=None, by=None):
    staff.balance = money((staff.balance or 0) + amount)
    e = LedgerEntry(staff_id=staff.id, kind=kind, amount=money(amount), balance_after=staff.balance, note=note,
                    customer_id=customer.id if customer else None, by_staff_id=by.id if by else None)
    db.session.add(e)
    return e


def topup(from_staff, to_staff, amount, note=""):
    """Parent tops up a child's wallet (mobile-operator style)."""
    amount = money(amount)
    if amount <= 0:
        raise BillingError("Amount must be positive")
    if to_staff.parent_id != from_staff.id and not from_staff.is_admin:
        raise BillingError("Not your sub-account")
    if not from_staff.is_admin:
        if not from_staff.can_spend(amount):
            raise BillingError("Insufficient balance")
        ledger(from_staff, "topup", -amount, f"Top-up to {to_staff.username}", by=from_staff)
    ledger(to_staff, "topup", amount, note or f"Top-up from {from_staff.username}", by=from_staff)
    audit("topup", f"{from_staff.username}->{to_staff.username} {amount}", actor=from_staff.username)


def receive_payment(by_staff, staff, amount, note=""):
    """Reseller/distributor pays their dues (postpaid) or advance to their upstream."""
    amount = money(amount)
    if amount <= 0:
        raise BillingError("Amount must be positive")
    ledger(staff, "payment", amount, note or "Payment received", by=by_staff)
    audit("staff_payment", f"{staff.username} paid {amount} (recorded by {by_staff.username})",
          actor=by_staff.username)


def recharge_by_staff(staff, customer, package):
    """Staff (reseller/distributor/admin) recharges a customer's package from their wallet."""
    price = package.price
    chain, node = [], staff
    while node and not node.is_admin:
        chain.append(node)
        node = node.parent
    cost = staff.cost_for(price)
    if not staff.is_admin and not staff.can_spend(cost):
        raise BillingError("Insufficient wallet balance - please top-up or settle dues")
    inv = create_invoice(customer, price, "package", package, due_days=0)
    net = cost if not chain else chain[-1].cost_for(price)
    if staff.is_admin:
        net = price
    if not staff.is_admin:
        ledger(staff, "recharge", -cost, f"Recharge {customer.username} / {package.name}", customer, by=staff)
        # upstream partners earn the difference between what the child paid and their own cost
        child_cost = cost
        for up in chain[1:]:
            up_cost = up.cost_for(price)
            ledger(up, "commission", child_cost - up_cost, f"Commission {customer.username}", customer)
            child_cost = up_cost
    p = record_payment(customer, inv, "reseller" if not staff.is_admin else "cash", price,
                       reseller=None if staff.is_admin else staff, net=net, note=f"by {staff.username}")
    audit("recharge", f"{staff.username} -> {customer.username} {package.name} cost={cost}", actor=staff.username)
    return p


# ----------------------------------------------------------------- monthly jobs
def generate_postpaid_invoices(period=None):
    period = period or f"{utcnow():%Y-%m}"
    made = []
    for c in Customer.query.filter(Customer.billing_type == "postpaid", Customer.status.in_(["active", "suspended"])):
        if Invoice.query.filter_by(customer_id=c.id, kind="monthly", period=period).first():
            continue
        amount = c.monthly_charge or (c.package.price if c.package else 0)
        if amount <= 0:
            continue
        inv = create_invoice(c, amount, "monthly", c.package, f"Internet bill {period}", period)
        notify(c, "invoice", amount=f"{amount:g}", invoice=inv.number, due=f"{inv.due_date:%d-%b}")
        made.append(inv)
    db.session.commit()
    return made


def charge_staff_subscriptions(period=None):
    period = period or f"{utcnow():%Y-%m}"
    n = 0
    for s in Staff.query.filter(Staff.role != "admin", Staff.active.is_(True)):
        if s.last_subscription_month == period:
            continue
        fee = s.monthly_fee or 0
        if s.billing_model == "per_user":
            active = Customer.query.filter(Customer.reseller_id.in_(s.subtree_ids()),
                                           Customer.status == "active").count()
            fee += active * (s.per_user_fee or 0)
        s.last_subscription_month = period
        if fee > 0:
            ledger(s, "subscription", -fee, f"Subscription {period}")
            n += 1
    db.session.commit()
    return n
