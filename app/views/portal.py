from datetime import timedelta

from flask import Blueprint, abort, flash, g, redirect, render_template, request, session, url_for

from .. import db
from ..models import Customer, InternetSession, Invoice, Package, Payment, Staff, Ticket, TicketMessage
from ..services import payments as pay
from ..services.billing import create_invoice
from ..services.core import audit, get_setting, login_failed, login_locked, login_ok
from ..services.provision import sync_customer
from ..utils import utcnow
from .guards import customer_required

bp = Blueprint("portal", __name__, url_prefix="/portal")
brand_bp = bp


def _brand(slug):
    return Staff.query.filter_by(brand_slug=slug).first() if slug else None


@bp.route("/login", methods=["GET", "POST"])
def login():
    return _login(None)


def _login(slug):
    brand = _brand(slug)
    if request.method == "POST":
        u = request.form.get("username", "").strip()
        if login_locked("cust", u):
            flash("Too many failed attempts. Try again later.", "error")
            return render_template("login.html", kind="customer", brand=brand), 429
        c = Customer.query.filter_by(username=u).first()
        if c and c.check_portal_password(request.form.get("password", "")):
            login_ok("cust", u)
            session.clear()
            session.permanent = True
            session["customer_id"] = c.id
            db.session.commit()
            return redirect(url_for("portal.dashboard"))
        login_failed("cust", u)
        flash("Invalid username or password", "error")
    return render_template("login.html", kind="customer", brand=brand)


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("portal.login"))


@bp.route("/")
@customer_required
def dashboard():
    c = g.customer
    online = InternetSession.query.filter_by(customer_id=c.id, ended_at=None).first()
    recent = Payment.query.filter_by(customer_id=c.id).order_by(Payment.id.desc()).limit(5).all()
    unpaid = Invoice.query.filter_by(customer_id=c.id, status="unpaid").order_by(Invoice.id.desc()).all()
    days_left = max(0, (c.expires_at - utcnow()).days) if c.expires_at else None
    return render_template("portal/dashboard.html", c=c, online=online, recent=recent, unpaid=unpaid,
                           days_left=days_left, methods=pay.available_methods(), brand=c.reseller if c.brand else None)


@bp.route("/packages")
@customer_required
def packages():
    pk = Package.query.filter_by(active=True, kind=g.customer.kind).order_by(Package.price).all()
    return render_template("portal/packages.html", packages=pk, methods=pay.available_methods(),
                           brand=g.customer.reseller if g.customer.brand else None)


@bp.route("/buy/<int:pid>", methods=["POST"])
@customer_required
def buy(pid):
    pkg = db.session.get(Package, pid)
    if not pkg or not pkg.active:
        abort(404)
    inv = create_invoice(g.customer, pkg.price, "package", pkg, due_days=0)
    return _start_pay(inv, request.form.get("method", ""))


@bp.route("/invoices/<int:iid>/pay", methods=["POST"])
@customer_required
def pay_invoice(iid):
    inv = Invoice.query.filter_by(id=iid, customer_id=g.customer.id, status="unpaid").first_or_404()
    return _start_pay(inv, request.form.get("method", ""))


def _start_pay(inv, method):
    if method not in [m[0] for m in pay.available_methods()]:
        flash("Select a valid payment method", "error")
        db.session.rollback()
        return redirect(url_for("portal.packages"))
    try:
        mode, url, p = pay.start(g.customer, inv, method)
    except pay.PaymentError as e:
        db.session.rollback()
        flash(str(e), "error")
        return redirect(url_for("portal.packages"))
    db.session.commit()
    return redirect(url if mode == "redirect" else url_for("portal.manual", pid=p.id))


@bp.route("/pay/<int:pid>/manual", methods=["GET", "POST"])
@customer_required
def manual(pid):
    p = Payment.query.filter_by(id=pid, customer_id=g.customer.id).first_or_404()
    if p.status == "success":
        return redirect(url_for("portal.history"))
    if request.method == "POST":
        try:
            pay.submit_manual(p, request.form.get("trx_id", ""), request.form.get("sender", ""))
            db.session.commit()
            flash("Payment activated!" if p.status == "success" else
                  "Submitted. Your service activates automatically once the payment is confirmed.", "ok")
            return redirect(url_for("portal.history"))
        except pay.PaymentError as e:
            flash(str(e), "error")
    return render_template("portal/manual.html", p=p, number=get_setting(f"{p.method}_number"),
                           label=pay.SEND_MONEY.get(p.method, p.method))


@bp.route("/history")
@customer_required
def history():
    c = g.customer
    return render_template(
        "portal/history.html",
        payments=Payment.query.filter_by(customer_id=c.id).order_by(Payment.id.desc()).limit(100).all(),
        invoices=Invoice.query.filter_by(customer_id=c.id).order_by(Invoice.id.desc()).limit(100).all(),
        sessions=InternetSession.query.filter_by(customer_id=c.id).order_by(InternetSession.id.desc()).limit(100).all())


@bp.route("/profile", methods=["GET", "POST"])
@customer_required
def profile():
    c = g.customer
    if request.method == "POST":
        f = request.form
        if f.get("action") == "password":
            if not c.check_portal_password(f.get("current", "")):
                flash("Current password is wrong", "error")
            elif len(f.get("new", "")) < 6 or f.get("new") != f.get("confirm"):
                flash("New password must be 6+ characters and match the confirmation", "error")
            else:
                c.set_portal_password(f["new"])
                c.service_password = f["new"]
                sync_customer(c)
                audit("customer_password", c.username, actor=c.username)
                db.session.commit()
                flash("Password changed (internet password updated too)", "ok")
        else:
            c.name, c.email, c.address = f.get("name", c.name).strip(), f.get("email", "").strip(), f.get("address", "").strip()
            db.session.commit()
            flash("Profile updated", "ok")
        return redirect(url_for("portal.profile"))
    return render_template("portal/profile.html", c=c)


@bp.route("/tickets", methods=["GET", "POST"])
@customer_required
def tickets():
    if request.method == "POST":
        subj, body = request.form.get("subject", "").strip(), request.form.get("body", "").strip()
        if not subj or not body:
            flash("Subject and message are required", "error")
        else:
            t = Ticket(customer_id=g.customer.id, subject=subj, category=request.form.get("category", "general"))
            t.messages.append(TicketMessage(author=g.customer.name, body=body))
            db.session.add(t)
            db.session.commit()
            flash(f"Ticket #{t.id} opened", "ok")
            return redirect(url_for("portal.ticket", tid=t.id))
    ts = Ticket.query.filter_by(customer_id=g.customer.id).order_by(Ticket.id.desc()).all()
    return render_template("portal/tickets.html", tickets=ts)


@bp.route("/tickets/<int:tid>", methods=["GET", "POST"])
@customer_required
def ticket(tid):
    t = Ticket.query.filter_by(id=tid, customer_id=g.customer.id).first_or_404()
    if request.method == "POST" and request.form.get("body", "").strip():
        t.messages.append(TicketMessage(author=g.customer.name, body=request.form["body"].strip()))
        t.status, t.updated_at = "open", utcnow()
        db.session.commit()
        return redirect(url_for("portal.ticket", tid=t.id))
    return render_template("portal/ticket.html", t=t)


@bp.route("/invoice/<int:iid>.pdf")
@customer_required
def invoice_pdf(iid):
    from .pay import invoice_pdf_response
    inv = Invoice.query.filter_by(id=iid, customer_id=g.customer.id).first_or_404()
    return invoice_pdf_response(inv)


# branded login: /r/<slug>
from flask import Blueprint as _B  # noqa: E402

brand_login_bp = _B("brand", __name__)


@brand_login_bp.route("/r/<slug>", methods=["GET", "POST"])
def brand_login(slug):
    if not _brand(slug):
        abort(404)
    return _login(slug)
