import re
from datetime import timedelta

from flask import (Blueprint, abort, flash, g, jsonify, redirect, render_template, request, session, url_for)
from sqlalchemy import func

from .. import db
from ..mikrotik import RouterError, get_client
from ..models import (AuditLog, Customer, InternetSession, Invoice, LedgerEntry, Package, Payment, ReceivedTxn,
                      Router, SmsLog, SmsTemplate, Staff, Ticket, TicketMessage)
from ..services import payments as pay
from ..services.billing import (BillingError, create_invoice, ledger, receive_payment, record_payment,
                                recharge_by_staff, topup)
from ..services.core import DEFAULT_SETTINGS, audit, get_setting, set_setting
from ..services.provision import check_router, disconnect, remove_customer, sync_customer
from ..services.sms import notify, send_sms
from ..utils import (csv_response, gen_password, local_day_range, local_month_range, money, norm_mac, norm_phone,
                     utcnow)
from .guards import get_customer_or_404, scoped_customers, staff_required

bp = Blueprint("staff", __name__, url_prefix="/staff")
ADMIN = ("admin",)
ANY = ()


def F(name, label, type="text", value="", options=None, required=False, help=""):
    return dict(name=name, label=label, type=type, value=value if value is not None else "", options=options or [],
                required=required, help=help)


def form_page(title, fields, back, action=None, submit="Save"):
    return render_template("staff/form.html", title=title, fields=fields, back=back, submit=submit)


def scoped_invoices(staff):
    q = Invoice.query.join(Customer, Customer.id == Invoice.customer_id)
    return q if staff.is_admin else q.filter(Customer.reseller_id.in_(staff.subtree_ids()))


def scoped_payments(staff):
    q = Payment.query.join(Customer, Customer.id == Payment.customer_id)
    return q if staff.is_admin else q.filter(Customer.reseller_id.in_(staff.subtree_ids()))


def visible_staff(staff):
    q = Staff.query.filter(Staff.role != "admin")
    if not staff.is_admin:
        q = q.filter(Staff.id.in_([i for i in staff.subtree_ids() if i != staff.id]))
    return q


# ============================================================== dashboard
@bp.route("/")
@staff_required()
def dashboard():
    s = g.staff
    custs = scoped_customers(s)
    counts = dict(db.session.query(Customer.status, func.count()).filter(
        Customer.id.in_(custs.with_entities(Customer.id))).group_by(Customer.status).all())
    ids = custs.with_entities(Customer.id)
    online = InternetSession.query.filter(InternetSession.ended_at.is_(None),
                                          InternetSession.customer_id.in_(ids)).count()

    def income(a, b):
        return money(scoped_payments(s).filter(Payment.status == "success", Payment.paid_at >= a, Payment.paid_at < b)
                     .with_entities(func.coalesce(func.sum(Payment.amount), 0)).scalar())

    t0, t1 = local_day_range()
    m0, m1 = local_month_range()
    days = []
    for i in range(13, -1, -1):
        a = t0 - timedelta(days=i)
        days.append(((a + timedelta(hours=current_hours())).strftime("%d %b"), income(a, a + timedelta(days=1))))
    due = money(scoped_invoices(s).filter(Invoice.status == "unpaid").with_entities(
        func.coalesce(func.sum(Invoice.amount), 0)).scalar())
    return render_template(
        "staff/dashboard.html", counts=counts, total=sum(counts.values()), online=online,
        today=income(t0, t1), month=income(m0, m1), days=days, max_day=max([d[1] for d in days] + [1]), due=due,
        review=scoped_payments(s).filter(Payment.status == "review").count(),
        open_tickets=Ticket.query.filter(Ticket.status != "closed", Ticket.customer_id.in_(ids)).count(),
        routers=Router.query.order_by(Router.name).all() if s.is_admin else [],
        recent=scoped_payments(s).filter(Payment.status == "success").order_by(Payment.id.desc()).limit(8).all())


def current_hours():
    from flask import current_app
    return current_app.config["TZ_OFFSET_HOURS"]


# ============================================================== customers
def customer_fields(c=None, staff=None):
    routers = [(r.id, f"{r.name} ({r.zone})") for r in Router.query.filter_by(active=True)]
    pk = [("", "- none -")] + [(p.id, f"{p.name} - {p.price:g}") for p in Package.query.filter_by(active=True)]
    f = [
        F("name", "Full name", value=c.name if c else "", required=True),
        F("phone", "Mobile", value=c.phone if c else "", required=True),
        F("email", "Email", value=c.email if c else ""),
        F("address", "Address", value=c.address if c else ""),
        F("username", "Login username", value=c.username if c else "", required=True,
          help="PPPoE / hotspot username (also portal login)"),
        F("kind", "Service", "select", c.kind if c else "pppoe", [("pppoe", "PPPoE"), ("hotspot", "Hotspot")]),
        F("router_id", "Router", "select", c.router_id if c else "", routers),
        F("billing_type", "Billing", "select", c.billing_type if c else "prepaid",
          [("prepaid", "Prepaid (recharge)"), ("postpaid", "Postpaid (monthly bill)")]),
        F("monthly_charge", "Postpaid monthly charge", "number", c.monthly_charge if c else 0,
          help="0 = use package price"),
    ]
    if not c:
        f.append(F("password", "Password", value=gen_password(), required=True))
        f.append(F("package_id", "Package", "select", "", pk))
        f.append(F("activate", "Activate now (charges your wallet)", "select", "1", [("1", "Yes"), ("0", "No")]))
        if staff and staff.is_admin:
            f.append(F("reseller_id", "Owner reseller", "select", "",
                       [("", "- direct (ISP) -")] + [(s.id, f"{s.name} ({s.role})") for s in Staff.query.filter(
                           Staff.role != "admin")]))
    return f


@bp.route("/customers")
@staff_required()
def customers():
    q = scoped_customers(g.staff).filter(Customer.status != "deleted")
    st, kw = request.args.get("status", ""), request.args.get("q", "").strip()
    if st:
        q = q.filter(Customer.status == st)
    if kw:
        like = f"%{kw}%"
        q = q.filter(db.or_(Customer.username.like(like), Customer.name.like(like), Customer.phone.like(like)))
    page = max(1, request.args.get("page", 1, type=int))
    total = q.count()
    rows = q.order_by(Customer.id.desc()).offset((page - 1) * 50).limit(50).all()
    online = {s.customer_id for s in InternetSession.query.filter(InternetSession.ended_at.is_(None))}
    return render_template("staff/customers.html", rows=rows, status=st, kw=kw, page=page, total=total, online=online)


@bp.route("/customers/new", methods=["GET", "POST"])
@staff_required()
def customer_new():
    s = g.staff
    if request.method == "POST":
        f = request.form
        username = re.sub(r"[^A-Za-z0-9_.@-]", "", f.get("username", ""))
        if not username or Customer.query.filter_by(username=username).first():
            flash("Username missing or already taken", "error")
        elif not f.get("router_id") or not f.get("name"):
            flash("Name and router are required", "error")
        else:
            owner_id = (int(f["reseller_id"]) if f.get("reseller_id") else None) if s.is_admin else s.id
            c = Customer(username=username, service_password=f["password"], name=f["name"].strip(),
                         phone=norm_phone(f.get("phone")), email=f.get("email", ""), address=f.get("address", ""),
                         kind=f.get("kind", "pppoe"), router_id=int(f["router_id"]), reseller_id=owner_id,
                         billing_type=f.get("billing_type", "prepaid"),
                         monthly_charge=money(f.get("monthly_charge")))
            c.set_portal_password(f["password"])
            pkg = db.session.get(Package, int(f["package_id"])) if f.get("package_id") else None
            if pkg:
                c.package, c.kind = pkg, pkg.kind
            db.session.add(c)
            db.session.flush()
            try:
                if pkg and f.get("activate") == "1":
                    if c.billing_type == "postpaid":
                        c.status = "active"
                        sync_customer(c)
                    else:
                        recharge_by_staff(s, c, pkg)
                else:
                    sync_customer(c)
                notify(c, "welcome", password=f["password"])
                audit("customer_create", c.username, actor=s.username)
                db.session.commit()
                flash(f"Customer {c.username} created", "ok")
                return redirect(url_for("staff.customer", cid=c.id))
            except BillingError as e:
                db.session.rollback()
                flash(str(e), "error")
    return form_page("New customer", customer_fields(staff=s), url_for("staff.customers"), submit="Create")


@bp.route("/customers/<int:cid>", methods=["GET", "POST"])
@staff_required()
def customer(cid):
    s, c = g.staff, get_customer_or_404(g.staff, cid)
    if request.method == "POST":
        return _customer_action(s, c)
    online = InternetSession.query.filter_by(customer_id=c.id, ended_at=None).all()
    return render_template(
        "staff/customer.html", c=c, online=online,
        packages=Package.query.filter_by(active=True, kind=c.kind).all(),
        invoices=Invoice.query.filter_by(customer_id=c.id).order_by(Invoice.id.desc()).limit(20).all(),
        payments=Payment.query.filter_by(customer_id=c.id).order_by(Payment.id.desc()).limit(20).all(),
        sessions=InternetSession.query.filter_by(customer_id=c.id).order_by(InternetSession.id.desc()).limit(15).all())


def _customer_action(s, c):
    f, act = request.form, request.form.get("action", "")
    try:
        if act == "recharge":
            pkg = db.get_or_404(Package, int(f.get("package_id", 0)))
            recharge_by_staff(s, c, pkg)
            flash(f"Recharged {pkg.name}", "ok")
        elif act == "suspend":
            c.status = "suspended"
            sync_customer(c)
            flash("Suspended", "ok")
        elif act == "unsuspend":
            c.status = "active" if (c.billing_type == "postpaid" or (c.expires_at and c.expires_at > utcnow())) \
                else "expired"
            sync_customer(c)
            flash("Status restored: " + c.status, "ok")
        elif act == "disconnect":
            flash("Disconnected" if disconnect(c) else "Router unreachable", "ok")
        elif act == "sync":
            flash("Synced to router" if sync_customer(c) else f"Sync failed: {c.sync_error}", "ok")
        elif act == "password":
            pw = f.get("password") or gen_password()
            c.service_password = pw
            c.set_portal_password(pw)
            sync_customer(c)
            flash(f"New password: {pw}", "ok")
        elif act == "binding":
            mac = norm_mac(f.get("mac"))
            if f.get("mac") and not mac:
                raise BillingError("Invalid MAC address")
            c.bound_mac, c.bound_ip = mac, f.get("ip", "").strip()
            sync_customer(c)
            audit("ip_binding", f"{c.username} {mac} {c.bound_ip}", actor=s.username)
            flash("IP/MAC binding saved" if mac else "Binding removed", "ok")
        elif act == "roaming":
            c.roaming = not c.roaming
            sync_customer(c)
            flash(f"Roaming {'enabled' if c.roaming else 'disabled'}", "ok")
        elif act == "edit":
            c.name, c.email, c.address = f["name"].strip(), f.get("email", ""), f.get("address", "")
            c.phone = norm_phone(f.get("phone"))
            c.billing_type = f.get("billing_type", c.billing_type)
            c.monthly_charge = money(f.get("monthly_charge"))
            if f.get("router_id") and int(f["router_id"]) != c.router_id:
                remove_customer(c)
                c.router_id = int(f["router_id"])
            sync_customer(c)
            flash("Saved", "ok")
        elif act == "collect":  # cash payment for an unpaid invoice
            inv = Invoice.query.filter_by(id=int(f.get("invoice_id", 0)), customer_id=c.id, status="unpaid").first_or_404()
            record_payment(c, inv, "cash", inv.amount, reseller=None if s.is_admin else s, note=f"by {s.username}")
            flash("Payment recorded", "ok")
        elif act == "new_invoice":
            amt = money(f.get("amount"))
            if amt <= 0:
                raise BillingError("Amount must be positive")
            create_invoice(c, amt, "other", description=f.get("description", "Charge"))
            flash("Invoice created", "ok")
        elif act == "delete":
            ok, errs = remove_customer(c)
            c.status, c.username = "deleted", f"{c.username}~del{c.id}"
            c.needs_sync = False
            audit("customer_delete", c.username, actor=s.username)
            db.session.commit()
            flash("Customer deleted" + ("" if ok else " (router cleanup incomplete: " + "; ".join(errs) + ")"), "ok")
            return redirect(url_for("staff.customers"))
        else:
            abort(400)
        audit(f"customer_{act}", c.username, actor=s.username)
        db.session.commit()
    except BillingError as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(url_for("staff.customer", cid=c.id))


# ============================================================== packages (admin)
def package_fields(p=None):
    return [
        F("name", "Name", value=p.name if p else "", required=True),
        F("kind", "Type", "select", p.kind if p else "pppoe", [("pppoe", "PPPoE"), ("hotspot", "Hotspot")]),
        F("price", "Price", "number", p.price if p else "", required=True),
        F("validity_days", "Validity (days)", "number", p.validity_days if p else 30, required=True),
        F("rate_limit", "Rate limit (rx/tx)", value=p.rate_limit if p else "", help="e.g. 10M/10M"),
        F("profile", "Router profile name", value=p.profile if p else "", help="empty = auto (pkg-<id>)"),
        F("data_limit_mb", "Data limit MB (hotspot, 0=unlimited)", "number", p.data_limit_mb if p else 0),
        F("description", "Description", value=p.description if p else ""),
        F("active", "Active", "select", "1" if (p is None or p.active) else "0", [("1", "Yes"), ("0", "No")]),
    ]


def _fill_package(p, f):
    p.name, p.kind, p.price = f["name"].strip(), f["kind"], money(f["price"])
    p.validity_days, p.rate_limit = int(f["validity_days"] or 30), f.get("rate_limit", "").strip()
    p.profile, p.data_limit_mb = f.get("profile", "").strip(), int(f.get("data_limit_mb") or 0)
    p.description, p.active = f.get("description", ""), f.get("active") == "1"


@bp.route("/packages")
@staff_required(*ADMIN)
def packages():
    return render_template("staff/packages.html", rows=Package.query.order_by(Package.kind, Package.price).all())


@bp.route("/packages/new", methods=["GET", "POST"])
@bp.route("/packages/<int:pid>", methods=["GET", "POST"])
@staff_required(*ADMIN)
def package_edit(pid=None):
    p = db.get_or_404(Package, pid) if pid else None
    if request.method == "POST":
        try:
            p = p or Package()
            _fill_package(p, request.form)
            db.session.add(p)
            db.session.commit()
            flash("Package saved", "ok")
            return redirect(url_for("staff.packages"))
        except (ValueError, KeyError):
            db.session.rollback()
            flash("Invalid values", "error")
    return form_page("Package", package_fields(p), url_for("staff.packages"))


# ============================================================== routers (admin)
def router_fields(r=None):
    return [
        F("name", "Name", value=r.name if r else "", required=True),
        F("host", "Host / IP", value=r.host if r else "", required=True),
        F("port", "API port", "number", r.port if r else 8728),
        F("username", "API username", value=r.username if r else "admin"),
        F("password", "API password", "password", "", help="leave blank to keep current" if r else ""),
        F("use_ssl", "API-SSL (8729)", "select", "1" if r and r.use_ssl else "0", [("0", "No"), ("1", "Yes")]),
        F("zone", "Zone", value=r.zone if r else "default"),
        F("allow_roaming", "Allow hotspot roaming here", "select", "0" if r and not r.allow_roaming else "1",
          [("1", "Yes"), ("0", "No")]),
        F("hotspot_server", "Hotspot server name", value=r.hotspot_server if r else "", help="optional"),
        F("mock", "Simulated router (demo)", "select", "1" if r and r.mock else "0", [("0", "No"), ("1", "Yes")]),
        F("active", "Active", "select", "0" if r and not r.active else "1", [("1", "Yes"), ("0", "No")]),
    ]


@bp.route("/routers")
@staff_required(*ADMIN)
def routers():
    return render_template("staff/routers.html", rows=Router.query.order_by(Router.name).all())


@bp.route("/routers/new", methods=["GET", "POST"])
@bp.route("/routers/<int:rid>", methods=["GET", "POST"])
@staff_required(*ADMIN)
def router_edit(rid=None):
    r = db.get_or_404(Router, rid) if rid else None
    if request.method == "POST":
        f = request.form
        try:
            r = r or Router()
            r.name, r.host, r.port = f["name"].strip(), f["host"].strip(), int(f.get("port") or 8728)
            r.username = f.get("username", "admin")
            if f.get("password"):
                r.password = f["password"]
            r.use_ssl, r.zone = f.get("use_ssl") == "1", f.get("zone", "default")
            r.allow_roaming, r.hotspot_server = f.get("allow_roaming") == "1", f.get("hotspot_server", "")
            r.mock, r.active = f.get("mock") == "1", f.get("active") == "1"
            db.session.add(r)
            db.session.commit()
            flash("Router saved", "ok")
            return redirect(url_for("staff.routers"))
        except Exception as e:
            db.session.rollback()
            flash(f"Could not save: {e}", "error")
    return form_page("Router", router_fields(r), url_for("staff.routers"))


@bp.route("/routers/<int:rid>/test", methods=["POST"])
@staff_required(*ADMIN)
def router_test(rid):
    r = db.get_or_404(Router, rid)
    info = check_router(r)
    flash(f"Connected: {info['identity']} RouterOS {info['version']}" if info else f"Failed: {r.last_error}",
          "ok" if info else "error")
    return redirect(url_for("staff.routers"))


@bp.route("/routers/<int:rid>/delete", methods=["POST"])
@staff_required(*ADMIN)
def router_delete(rid):
    r = db.get_or_404(Router, rid)
    if Customer.query.filter_by(router_id=r.id).filter(Customer.status != "deleted").count():
        flash("Router still has customers - move them first", "error")
    else:
        db.session.delete(r)
        db.session.commit()
        flash("Router deleted", "ok")
    return redirect(url_for("staff.routers"))


@bp.route("/monitor")
@staff_required(*ADMIN)
def monitor():
    rid = request.args.get("router", type=int)
    routers = Router.query.filter_by(active=True).order_by(Router.name).all()
    r = db.session.get(Router, rid) if rid else (routers[0] if routers else None)
    logs = []
    if r:
        try:
            with get_client(__import__("flask").current_app, r) as c:
                logs = c.logs("hotspot", 40)
        except RouterError as e:
            flash(f"Router unreachable: {e}", "error")
    hist = InternetSession.query.order_by(InternetSession.id.desc()).limit(60).all()
    return render_template("staff/monitor.html", routers=routers, r=r, logs=logs, hist=hist)


# ============================================================== staff (distributors / resellers)
def staff_fields(st=None, actor=None):
    f = [
        F("name", "Name", value=st.name if st else "", required=True),
        F("username", "Login username", value=st.username if st else "", required=True),
        F("password", "Password", "password", "", help="leave blank to keep" if st else "", required=not st),
        F("phone", "Phone", value=st.phone if st else ""),
        F("email", "Email", value=st.email if st else ""),
    ]
    if not st:
        roles = [("reseller", "Reseller")] + ([("distributor", "Distributor")] if actor.is_admin else [])
        f.append(F("role", "Role", "select", "reseller", roles))
        if actor.is_admin:
            f.append(F("parent_id", "Parent", "select", "", [("", "- ISP (direct) -")] + [
                (s.id, f"{s.name} ({s.role})") for s in Staff.query.filter_by(role="distributor")]))
    f += [
        F("payment_mode", "Payment mode", "select", st.payment_mode if st else "prepaid",
          [("prepaid", "Prepaid (wallet must cover recharge)"), ("postpaid", "Postpaid (pay later, up to credit limit)")]),
        F("credit_limit", "Credit limit (postpaid)", "number", st.credit_limit if st else 0),
        F("billing_model", "Business model", "select", st.billing_model if st else "commission",
          [("commission", "Commission (discount on package price)"), ("per_user", "Per-user subscription")]),
        F("commission_pct", "Commission % (discount)", "number", st.commission_pct if st else 0),
        F("per_user_fee", "Fee per active user / month", "number", st.per_user_fee if st else 0),
        F("monthly_fee", "Flat monthly subscription", "number", st.monthly_fee if st else 0),
        F("brand_name", "Brand name (white-label)", value=st.brand_name if st else ""),
        F("brand_slug", "Brand URL slug", value=st.brand_slug if st else "", help="portal at /r/<slug>"),
        F("brand_color", "Brand color (#hex)", value=st.brand_color if st else ""),
        F("support_phone", "Support phone", value=st.support_phone if st else ""),
    ]
    if st:
        f.append(F("active", "Active", "select", "1" if st.active else "0", [("1", "Yes"), ("0", "No")]))
    return f


@bp.route("/team")
@staff_required("admin", "distributor")
def team():
    return render_template("staff/team.html", rows=visible_staff(g.staff).order_by(Staff.role, Staff.name).all())


@bp.route("/team/new", methods=["GET", "POST"])
@bp.route("/team/<int:sid>/edit", methods=["GET", "POST"])
@staff_required("admin", "distributor")
def team_edit(sid=None):
    actor = g.staff
    st = visible_staff(actor).filter(Staff.id == sid).first_or_404() if sid else None
    if request.method == "POST":
        f = request.form
        try:
            uname = re.sub(r"[^A-Za-z0-9_.-]", "", f["username"])
            clash = Staff.query.filter(Staff.username == uname, Staff.id != (st.id if st else 0)).first()
            slug = re.sub(r"[^a-z0-9-]", "", f.get("brand_slug", "").lower()) or None
            slug_clash = slug and Staff.query.filter(Staff.brand_slug == slug, Staff.id != (st.id if st else 0)).first()
            if not uname or clash or slug_clash:
                raise ValueError("Username or brand slug already used")
            if not st and not f.get("password"):
                raise ValueError("Password required")
            parent = actor
            if not st and actor.is_admin and f.get("parent_id"):
                parent = db.get_or_404(Staff, int(f["parent_id"]))
            elif not st and actor.is_admin:
                parent = actor
            pct = float(f.get("commission_pct") or 0)
            base = st.parent if st else parent
            if base and not base.is_admin and pct < (base.commission_pct or 0):
                raise ValueError(f"Commission cannot be lower than the parent's ({base.commission_pct}%)")
            if not 0 <= pct <= 100:
                raise ValueError("Commission must be 0-100")
            if not st:
                role = f.get("role", "reseller")
                if role == "distributor" and not actor.is_admin:
                    abort(403)
                st = Staff(role=role, parent=parent)
            st.username, st.name, st.phone, st.email = uname, f["name"].strip(), norm_phone(f.get("phone")), f.get("email", "")
            if f.get("password"):
                st.set_password(f["password"])
            st.payment_mode, st.billing_model = f.get("payment_mode", "prepaid"), f.get("billing_model", "commission")
            st.credit_limit, st.commission_pct = money(f.get("credit_limit")), pct
            st.per_user_fee, st.monthly_fee = money(f.get("per_user_fee")), money(f.get("monthly_fee"))
            st.brand_name, st.brand_slug = f.get("brand_name", ""), slug
            st.brand_color, st.support_phone = f.get("brand_color", ""), f.get("support_phone", "")
            if "active" in f:
                st.active = f["active"] == "1"
            db.session.add(st)
            audit("staff_save", st.username, actor=actor.username)
            db.session.commit()
            flash("Saved", "ok")
            return redirect(url_for("staff.team_view", sid=st.id))
        except (ValueError, KeyError) as e:
            db.session.rollback()
            flash(str(e), "error")
    return form_page("Distributor / Reseller", staff_fields(st, actor), url_for("staff.team"))


@bp.route("/team/<int:sid>", methods=["GET", "POST"])
@staff_required("admin", "distributor")
def team_view(sid):
    actor = g.staff
    st = visible_staff(actor).filter(Staff.id == sid).first_or_404()
    if request.method == "POST":
        try:
            amount, act = money(request.form.get("amount")), request.form.get("action")
            note = request.form.get("note", "")
            if act == "topup":
                if st.parent_id != actor.id and not actor.is_admin:
                    abort(403)
                topup(actor, st, amount, note)
            elif act == "payment":
                receive_payment(actor, st, amount, note)
            db.session.commit()
            flash("Done", "ok")
        except BillingError as e:
            db.session.rollback()
            flash(str(e), "error")
        return redirect(url_for("staff.team_view", sid=sid))
    entries = LedgerEntry.query.filter_by(staff_id=st.id).order_by(LedgerEntry.id.desc()).limit(100).all()
    n_cust = Customer.query.filter(Customer.reseller_id.in_(st.subtree_ids()), Customer.status != "deleted").count()
    return render_template("staff/team_view.html", st=st, entries=entries, n_cust=n_cust)


@bp.route("/wallet")
@staff_required()
def wallet():
    entries = LedgerEntry.query.filter_by(staff_id=g.staff.id).order_by(LedgerEntry.id.desc()).limit(200).all()
    return render_template("staff/wallet.html", st=g.staff, entries=entries)


# ============================================================== invoices / payments
@bp.route("/invoices")
@staff_required()
def invoices():
    q = scoped_invoices(g.staff)
    st = request.args.get("status", "")
    if st:
        q = q.filter(Invoice.status == st)
    return render_template("staff/invoices.html", rows=q.order_by(Invoice.id.desc()).limit(300).all(), status=st)


@bp.route("/invoices/<int:iid>")
@staff_required()
def invoice(iid):
    inv = scoped_invoices(g.staff).filter(Invoice.id == iid).first_or_404()
    return render_template("staff/invoice.html", inv=inv, addr=get_setting("company_address"),
                           phone=get_setting("company_phone"))


@bp.route("/invoices/<int:iid>.pdf")
@staff_required()
def invoice_pdf(iid):
    from .pay import invoice_pdf_response
    return invoice_pdf_response(scoped_invoices(g.staff).filter(Invoice.id == iid).first_or_404())


@bp.route("/payments")
@staff_required()
def payments():
    q = scoped_payments(g.staff)
    st = request.args.get("status", "")
    if st:
        q = q.filter(Payment.status == st)
    return render_template("staff/payments.html", rows=q.order_by(Payment.id.desc()).limit(300).all(), status=st,
                           orphans=ReceivedTxn.query.filter(ReceivedTxn.claimed_by.is_(None)).order_by(
                               ReceivedTxn.id.desc()).limit(20).all() if g.staff.is_admin else [])


@bp.route("/payments/<int:pid>/<action>", methods=["POST"])
@staff_required()
def payment_action(pid, action):
    p = scoped_payments(g.staff).filter(Payment.id == pid).first_or_404()
    if p.status not in ("review", "pending"):
        flash("Payment already processed", "error")
    elif action == "approve":
        pay.approve_manually(p, g.staff)
        flash("Approved - service activated", "ok")
    elif action == "reject":
        pay.reject(p, g.staff)
        flash("Rejected", "ok")
    db.session.commit()
    return redirect(request.referrer or url_for("staff.payments"))


# ============================================================== tickets
@bp.route("/tickets")
@staff_required()
def tickets():
    q = Ticket.query.filter(Ticket.customer_id.in_(scoped_customers(g.staff).with_entities(Customer.id)))
    st = request.args.get("status", "")
    if st:
        q = q.filter(Ticket.status == st)
    return render_template("staff/tickets.html", rows=q.order_by(Ticket.updated_at.desc()).limit(200).all(), status=st)


@bp.route("/tickets/<int:tid>", methods=["GET", "POST"])
@staff_required()
def ticket(tid):
    t = db.get_or_404(Ticket, tid)
    get_customer_or_404(g.staff, t.customer_id)
    if request.method == "POST":
        body, status = request.form.get("body", "").strip(), request.form.get("status", t.status)
        if body:
            t.messages.append(TicketMessage(from_staff=True, author=g.staff.name, body=body))
            notify(t.customer, "ticket_reply", ticket=t.id)
        t.status, t.updated_at = status, utcnow()
        db.session.commit()
        return redirect(url_for("staff.ticket", tid=tid))
    return render_template("staff/ticket.html", t=t)


# ============================================================== SMS
@bp.route("/sms", methods=["GET", "POST"])
@staff_required()
def sms():
    if request.method == "POST":
        msg = request.form.get("message", "").strip()
        q = scoped_customers(g.staff).filter(Customer.status != "deleted")
        if request.form.get("audience"):
            q = q.filter(Customer.status == request.form["audience"])
        if not msg:
            flash("Message is empty", "error")
        else:
            n = sum(1 for c in q if c.phone and send_sms(c.phone, msg, "bulk", c.id))
            db.session.commit()
            flash(f"{n} message(s) queued/sent", "ok")
        return redirect(url_for("staff.sms"))
    return render_template("staff/sms.html", rows=SmsLog.query.order_by(SmsLog.id.desc()).limit(150).all(),
                           templates=SmsTemplate.query.order_by(SmsTemplate.event).all())


@bp.route("/sms/template/<event>", methods=["POST"])
@staff_required(*ADMIN)
def sms_template(event):
    t = db.get_or_404(SmsTemplate, event)
    t.body, t.enabled = request.form.get("body", t.body), request.form.get("enabled") == "1"
    db.session.commit()
    flash("Template saved", "ok")
    return redirect(url_for("staff.sms"))


# ============================================================== reports
def _range():
    m0, _ = local_month_range()
    off = timedelta(hours=current_hours())
    try:
        a = __import__("datetime").datetime.strptime(request.args["from"], "%Y-%m-%d")
        b = __import__("datetime").datetime.strptime(request.args["to"], "%Y-%m-%d") + timedelta(days=1)
        return a - off, b - off
    except (KeyError, ValueError):
        return m0, utcnow() + timedelta(days=1)


@bp.route("/reports/income")
@staff_required()
def report_income():
    a, b = _range()
    rows = scoped_payments(g.staff).filter(Payment.status == "success", Payment.paid_at >= a, Payment.paid_at < b).all()
    off = timedelta(hours=current_hours())
    by_day, by_method = {}, {}
    for p in rows:
        d = (p.paid_at + off).strftime("%Y-%m-%d")
        x = by_day.setdefault(d, [0, 0, 0])
        x[0] += p.amount
        x[1] += p.net_amount
        x[2] += 1
        by_method[p.method] = by_method.get(p.method, 0) + p.amount
    days = sorted(by_day.items())
    if request.args.get("format") == "csv":
        return csv_response("income.csv", ["Date", "Sales", "Net to ISP", "Payments"],
                            [(d, v[0], v[1], v[2]) for d, v in days])
    sub = 0
    if g.staff.is_admin:
        sub = -sum(e.amount for e in LedgerEntry.query.filter(LedgerEntry.kind == "subscription",
                                                              LedgerEntry.created_at >= a, LedgerEntry.created_at < b))
    return render_template("staff/report_income.html", days=days, by_method=sorted(by_method.items()),
                           total=sum(v[0] for _, v in days), net=sum(v[1] for _, v in days), sub=sub,
                           frm=request.args.get("from", ""), to=request.args.get("to", ""))


@bp.route("/reports/users")
@staff_required()
def report_users():
    q = scoped_customers(g.staff).filter(Customer.status != "deleted")
    st = request.args.get("status", "")
    if st:
        q = q.filter(Customer.status == st)
    rows = q.order_by(Customer.id).all()
    if request.args.get("format") == "csv":
        return csv_response("users.csv", ["Username", "Name", "Phone", "Type", "Package", "Status", "Expires", "Due"],
                            [(c.username, c.name, c.phone, c.kind, c.package.name if c.package else "", c.status,
                              c.expires_at.strftime("%Y-%m-%d") if c.expires_at else "", c.due_amount) for c in rows])
    return render_template("staff/report_users.html", rows=rows, status=st)


@bp.route("/reports/payments")
@staff_required()
def report_payments():
    a, b = _range()
    rows = scoped_payments(g.staff).filter(Payment.created_at >= a, Payment.created_at < b).order_by(Payment.id).all()
    if request.args.get("format") == "csv":
        return csv_response("payments.csv", ["Date", "Customer", "Method", "TrxID", "Amount", "Net", "Status"],
                            [(p.created_at.strftime("%Y-%m-%d %H:%M"), p.customer.username, p.method, p.trx_id,
                              p.amount, p.net_amount, p.status) for p in rows])
    return render_template("staff/report_payments.html", rows=rows, frm=request.args.get("from", ""),
                           to=request.args.get("to", ""))


# ============================================================== settings / security (admin)
SETTING_GROUPS = [
    ("Company", ["company_name", "company_phone", "company_address", "currency"]),
    ("Billing", ["invoice_due_days", "bill_day", "grace_days", "expiry_warn_days"]),
    ("Send-money numbers", ["bkash_number", "nagad_number", "rocket_number", "upay_number", "manual_enabled",
                             "sms_webhook_token"]),
    ("bKash API", ["bkash_enabled", "bkash_base", "bkash_app_key", "bkash_app_secret", "bkash_username",
                   "bkash_password"]),
    ("SSLCommerz", ["sslcommerz_enabled", "sslcommerz_sandbox", "sslcommerz_store_id", "sslcommerz_store_pass"]),
    ("SMS gateway", ["sms_mode", "sms_url", "sms_method", "sms_params"]),
    ("Testing", ["sandbox_enabled"]),
]
SECRETS = {"bkash_app_secret", "bkash_password", "sslcommerz_store_pass", "sms_webhook_token"}


@bp.route("/settings", methods=["GET", "POST"])
@staff_required(*ADMIN)
def settings():
    if request.method == "POST":
        for _, keys in SETTING_GROUPS:
            for k in keys:
                if k in request.form:
                    v = request.form[k]
                    if k in SECRETS and not v:
                        continue  # blank secret = keep
                    set_setting(k, v.strip())
        if request.form.get("gen_token"):
            import secrets as _s
            set_setting("sms_webhook_token", _s.token_urlsafe(24))
        audit("settings_saved", "", actor=g.staff.username)
        db.session.commit()
        flash("Settings saved", "ok")
        return redirect(url_for("staff.settings"))
    vals = {k: ("" if k in SECRETS else get_setting(k)) for _, ks in SETTING_GROUPS for k in ks}
    return render_template("staff/settings.html", groups=SETTING_GROUPS, vals=vals, secrets=SECRETS,
                           token_set=bool(get_setting("sms_webhook_token")))


@bp.route("/security")
@staff_required(*ADMIN)
def security():
    level = request.args.get("level", "")
    q = AuditLog.query
    if level:
        q = q.filter_by(level=level)
    bad = Customer.query.filter_by(needs_sync=True).filter(Customer.status != "deleted").all()
    return render_template("staff/security.html", rows=q.order_by(AuditLog.id.desc()).limit(200).all(), level=level,
                           bad=bad)
