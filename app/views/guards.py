from functools import wraps

from flask import abort, flash, g, redirect, request, session, url_for

from .. import db
from ..models import Customer, Staff


def load_actor():
    g.staff = db.session.get(Staff, session["staff_id"]) if "staff_id" in session else None
    g.customer = db.session.get(Customer, session["customer_id"]) if "customer_id" in session else None
    if g.staff and not g.staff.active:
        g.staff = None
        session.pop("staff_id", None)


def staff_required(*roles):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            load_actor()
            if not g.staff:
                return redirect(url_for("auth.staff_login", next=request.path))
            if roles and g.staff.role not in roles:
                abort(403)
            return fn(*a, **kw)
        return wrapper
    return deco


def customer_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        load_actor()
        if not g.customer:
            return redirect(url_for("portal.login"))
        return fn(*a, **kw)
    return wrapper


def scoped_customers(staff):
    q = Customer.query
    if not staff.is_admin:
        q = q.filter(Customer.reseller_id.in_(staff.subtree_ids()))
    return q


def get_customer_or_404(staff, cid):
    c = scoped_customers(staff).filter(Customer.id == cid).first()
    if not c:
        abort(404)
    return c
