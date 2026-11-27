"""Push customer state to MikroTik routers (PPPoE secrets / hotspot users / IP binding)."""
from flask import current_app

from .. import db
from ..mikrotik import RouterError, get_client
from ..models import Router
from ..utils import utcnow
from .core import audit


def routers_for(customer):
    """Home router plus, for roaming hotspot users, every roaming-enabled router."""
    rs = {}
    if customer.router and customer.router.active:
        rs[customer.router.id] = customer.router
    if customer.kind == "hotspot" and customer.roaming:
        for r in Router.query.filter_by(active=True, allow_roaming=True):
            rs[r.id] = r
    return list(rs.values())


def _mark(customer, ok, err=""):
    customer.needs_sync = not ok
    customer.sync_error = "" if ok else err[:250]


def sync_customer(customer):
    """Make routers match the DB. Returns True when every router accepted the change."""
    app = current_app._get_current_object()
    disabled = customer.status != "active"
    pkg = customer.package
    profile = pkg.profile_name if pkg else "default"
    errors = []
    for r in routers_for(customer):
        try:
            with get_client(app, r) as c:
                if pkg:
                    c.ensure_profile(customer.kind, profile, pkg.rate_limit)
                limit = (pkg.data_limit_mb or 0) * 1024 * 1024 if pkg else 0
                c.upsert_user(customer.kind, customer.username, customer.service_password, profile,
                              disabled, mac=customer.bound_mac if customer.kind == "pppoe" else "",
                              ip="", limit_bytes=limit)
                if customer.kind == "hotspot":
                    if customer.bound_mac:
                        c.set_ip_binding(customer.bound_mac, customer.bound_ip, f"cust-{customer.id}")
                    else:
                        c.remove_ip_binding(f"cust-{customer.id}")
                if disabled:
                    c.kick(customer.kind, customer.username)
        except RouterError as e:
            errors.append(f"{r.name}: {e}")
            audit("router_sync_failed", f"{customer.username} @ {r.name}: {e}", level="security")
    if not routers_for(customer):
        errors.append("no router assigned")
    _mark(customer, not errors, "; ".join(errors))
    return not errors


def remove_customer(customer):
    app = current_app._get_current_object()
    targets = {r.id: r for r in routers_for(customer)}
    if customer.kind == "hotspot":
        for r in Router.query.filter_by(active=True):
            targets[r.id] = r
    errors = []
    for r in targets.values():
        try:
            with get_client(app, r) as c:
                c.kick(customer.kind, customer.username)
                c.remove_user(customer.kind, customer.username)
                if customer.kind == "hotspot":
                    c.remove_ip_binding(f"cust-{customer.id}")
        except RouterError as e:
            errors.append(f"{r.name}: {e}")
    return not errors, errors


def disconnect(customer):
    app = current_app._get_current_object()
    ok = True
    for r in routers_for(customer):
        try:
            with get_client(app, r) as c:
                c.kick(customer.kind, customer.username)
        except RouterError:
            ok = False
    return ok


def check_router(router):
    app = current_app._get_current_object()
    try:
        with get_client(app, router) as c:
            info = c.resources()
        router.last_ok, router.last_error = True, ""
        return info
    except RouterError as e:
        router.last_ok, router.last_error = False, str(e)[:250]
        return None
    finally:
        router.last_checked = utcnow()
        db.session.commit()
