from werkzeug.security import check_password_hash, generate_password_hash

from . import db
from .utils import utcnow


class Setting(db.Model):
    key = db.Column(db.String(80), primary_key=True)
    value = db.Column(db.Text, default="")


class Staff(db.Model):
    """Admin, distributor or reseller account."""

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(60), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(20), default="")
    email = db.Column(db.String(120), default="")
    role = db.Column(db.String(20), default="reseller")  # admin | distributor | reseller
    parent_id = db.Column(db.Integer, db.ForeignKey("staff.id"))
    active = db.Column(db.Boolean, default=True)

    # wallet / business model
    balance = db.Column(db.Float, default=0.0)
    payment_mode = db.Column(db.String(10), default="prepaid")  # prepaid | postpaid
    credit_limit = db.Column(db.Float, default=0.0)  # max negative balance for postpaid
    billing_model = db.Column(db.String(12), default="commission")  # commission | per_user
    commission_pct = db.Column(db.Float, default=0.0)  # discount on package price
    per_user_fee = db.Column(db.Float, default=0.0)  # per active customer / month
    monthly_fee = db.Column(db.Float, default=0.0)  # flat subscription / month
    last_subscription_month = db.Column(db.String(7), default="")

    # white-label branding
    brand_name = db.Column(db.String(120), default="")
    brand_slug = db.Column(db.String(60), unique=True)
    brand_color = db.Column(db.String(9), default="")
    brand_logo = db.Column(db.String(255), default="")
    support_phone = db.Column(db.String(30), default="")

    created_at = db.Column(db.DateTime, default=utcnow)

    parent = db.relationship("Staff", remote_side=[id], backref="children")

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)

    @property
    def is_admin(self):
        return self.role == "admin"

    @property
    def display_brand(self):
        return self.brand_name or self.name

    def subtree_ids(self):
        ids, stack = [], [self]
        while stack:
            s = stack.pop()
            ids.append(s.id)
            stack.extend(s.children)
        return ids

    def cost_for(self, price):
        """What this reseller pays the upstream for a recharge worth `price`."""
        if self.billing_model == "per_user":
            return 0.0
        return round(price * (1 - (self.commission_pct or 0) / 100.0), 2)

    def can_spend(self, amount):
        if self.is_admin:
            return True
        after = (self.balance or 0) - amount
        if self.payment_mode == "postpaid":
            return after >= -(self.credit_limit or 0)
        return after >= 0

    @property
    def due(self):
        return round(max(0.0, -(self.balance or 0)), 2)


class Router(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    host = db.Column(db.String(120), nullable=False)
    port = db.Column(db.Integer, default=8728)
    username = db.Column(db.String(60), default="admin")
    password = db.Column(db.String(120), default="")
    use_ssl = db.Column(db.Boolean, default=False)
    zone = db.Column(db.String(60), default="default")
    allow_roaming = db.Column(db.Boolean, default=True)
    hotspot_server = db.Column(db.String(60), default="")
    mock = db.Column(db.Boolean, default=False)
    active = db.Column(db.Boolean, default=True)
    last_ok = db.Column(db.Boolean)
    last_error = db.Column(db.String(255), default="")
    last_checked = db.Column(db.DateTime)


class Package(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    kind = db.Column(db.String(10), default="pppoe")  # pppoe | hotspot
    price = db.Column(db.Float, nullable=False)
    validity_days = db.Column(db.Integer, default=30)
    rate_limit = db.Column(db.String(40), default="")  # MikroTik format rx/tx e.g. 10M/10M
    profile = db.Column(db.String(60), default="")  # router profile name (defaults to slug of name)
    data_limit_mb = db.Column(db.Integer, default=0)  # 0 = unlimited (hotspot)
    description = db.Column(db.String(255), default="")
    active = db.Column(db.Boolean, default=True)

    @property
    def profile_name(self):
        return self.profile or f"pkg-{self.id}"


class Customer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(60), unique=True, nullable=False)  # PPPoE / hotspot login
    service_password = db.Column(db.String(60), nullable=False)  # pushed to router
    portal_password_hash = db.Column(db.String(255), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(20), default="")
    email = db.Column(db.String(120), default="")
    address = db.Column(db.String(255), default="")
    kind = db.Column(db.String(10), default="pppoe")
    status = db.Column(db.String(12), default="pending")  # pending|active|expired|suspended
    billing_type = db.Column(db.String(10), default="prepaid")  # prepaid | postpaid
    monthly_charge = db.Column(db.Float, default=0.0)  # postpaid fixed charge override
    package_id = db.Column(db.Integer, db.ForeignKey("package.id"))
    router_id = db.Column(db.Integer, db.ForeignKey("router.id"))
    reseller_id = db.Column(db.Integer, db.ForeignKey("staff.id"))
    expires_at = db.Column(db.DateTime)
    roaming = db.Column(db.Boolean, default=True)
    # IP binding
    bound_mac = db.Column(db.String(17), default="")
    bound_ip = db.Column(db.String(45), default="")
    # router sync
    needs_sync = db.Column(db.Boolean, default=False)
    sync_error = db.Column(db.String(255), default="")
    expiry_notified = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=utcnow)

    package = db.relationship("Package")
    router = db.relationship("Router")
    reseller = db.relationship("Staff", backref="customers")

    def set_portal_password(self, pw):
        self.portal_password_hash = generate_password_hash(pw)

    def check_portal_password(self, pw):
        return check_password_hash(self.portal_password_hash, pw)

    @property
    def due_amount(self):
        return round(
            sum(i.amount for i in Invoice.query.filter_by(customer_id=self.id, status="unpaid")), 2
        )

    @property
    def brand(self):
        r = self.reseller
        return r.display_brand if r and r.brand_name else None


class Invoice(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(30), unique=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"), nullable=False)
    package_id = db.Column(db.Integer, db.ForeignKey("package.id"))
    kind = db.Column(db.String(12), default="package")  # package | monthly | other
    description = db.Column(db.String(255), default="")
    amount = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(10), default="unpaid")  # unpaid | paid | cancelled
    period = db.Column(db.String(7), default="")  # YYYY-MM for monthly invoices
    due_date = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=utcnow)
    paid_at = db.Column(db.DateTime)

    customer = db.relationship("Customer", backref="invoices")
    package = db.relationship("Package")


class Payment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"), nullable=False)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoice.id"))
    reseller_id = db.Column(db.Integer, db.ForeignKey("staff.id"))
    method = db.Column(db.String(20), nullable=False)  # bkash|nagad|rocket|upay|sslcommerz|sendmoney|cash|reseller|sandbox
    amount = db.Column(db.Float, nullable=False)  # what the customer paid
    net_amount = db.Column(db.Float, default=0.0)  # what reached the ISP (after reseller share)
    trx_id = db.Column(db.String(60), default="")
    sender = db.Column(db.String(30), default="")
    gateway_ref = db.Column(db.String(120), default="")  # gateway payment id / tran id
    status = db.Column(db.String(10), default="pending")  # pending|success|failed|review
    note = db.Column(db.String(255), default="")
    created_at = db.Column(db.DateTime, default=utcnow)
    paid_at = db.Column(db.DateTime)

    customer = db.relationship("Customer", backref="payments")
    invoice = db.relationship("Invoice", backref="payments")
    reseller = db.relationship("Staff")


class ReceivedTxn(db.Model):
    """Incoming Send-Money SMS forwarded from the merchant phone."""

    id = db.Column(db.Integer, primary_key=True)
    provider = db.Column(db.String(20), default="")
    trx_id = db.Column(db.String(60), unique=True, nullable=False)
    sender = db.Column(db.String(30), default="")
    amount = db.Column(db.Float, default=0.0)
    raw = db.Column(db.Text, default="")
    claimed_by = db.Column(db.Integer, db.ForeignKey("payment.id"))
    created_at = db.Column(db.DateTime, default=utcnow)


class LedgerEntry(db.Model):
    """Reseller / distributor wallet movements."""

    id = db.Column(db.Integer, primary_key=True)
    staff_id = db.Column(db.Integer, db.ForeignKey("staff.id"), nullable=False)
    kind = db.Column(db.String(15), nullable=False)  # topup|payment|recharge|commission|subscription|adjust
    amount = db.Column(db.Float, nullable=False)  # signed: + credit, - debit
    balance_after = db.Column(db.Float, default=0.0)
    note = db.Column(db.String(255), default="")
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"))
    by_staff_id = db.Column(db.Integer, db.ForeignKey("staff.id"))
    created_at = db.Column(db.DateTime, default=utcnow)

    staff = db.relationship("Staff", foreign_keys=[staff_id])
    by_staff = db.relationship("Staff", foreign_keys=[by_staff_id])


class SmsTemplate(db.Model):
    event = db.Column(db.String(30), primary_key=True)
    body = db.Column(db.Text, nullable=False)
    enabled = db.Column(db.Boolean, default=True)


class SmsLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    to = db.Column(db.String(20))
    message = db.Column(db.Text)
    event = db.Column(db.String(30), default="")
    status = db.Column(db.String(10), default="queued")  # sent|failed|logged
    detail = db.Column(db.String(255), default="")
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"))
    created_at = db.Column(db.DateTime, default=utcnow)


class Ticket(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"), nullable=False)
    subject = db.Column(db.String(160), nullable=False)
    category = db.Column(db.String(30), default="general")
    priority = db.Column(db.String(10), default="normal")
    status = db.Column(db.String(12), default="open")  # open|pending|closed
    assigned_to = db.Column(db.Integer, db.ForeignKey("staff.id"))
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow)

    customer = db.relationship("Customer", backref="tickets")
    messages = db.relationship("TicketMessage", backref="ticket", order_by="TicketMessage.id")


class TicketMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey("ticket.id"), nullable=False)
    from_staff = db.Column(db.Boolean, default=False)
    author = db.Column(db.String(120), default="")
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)


class InternetSession(db.Model):
    """Login history (hotspot log) built by polling routers."""

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"), index=True)
    router_id = db.Column(db.Integer, db.ForeignKey("router.id"))
    username = db.Column(db.String(60))
    kind = db.Column(db.String(10))
    ip = db.Column(db.String(45), default="")
    mac = db.Column(db.String(17), default="")
    started_at = db.Column(db.DateTime, default=utcnow)
    last_seen = db.Column(db.DateTime, default=utcnow)
    ended_at = db.Column(db.DateTime)
    bytes_in = db.Column(db.BigInteger, default=0)
    bytes_out = db.Column(db.BigInteger, default=0)

    customer = db.relationship("Customer", backref="sessions")
    router = db.relationship("Router")


class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    level = db.Column(db.String(10), default="info")  # info | security
    actor = db.Column(db.String(80), default="")
    action = db.Column(db.String(60), default="")
    detail = db.Column(db.String(500), default="")
    ip = db.Column(db.String(45), default="")
    created_at = db.Column(db.DateTime, default=utcnow)


class LoginAttempt(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(160), index=True)
    fails = db.Column(db.Integer, default=0)
    locked_until = db.Column(db.DateTime)
    updated_at = db.Column(db.DateTime, default=utcnow)
