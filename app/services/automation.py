"""Scheduled jobs: auto-expiry, suspension, reminders, session sync, security checks."""
import threading
import time
from datetime import timedelta

from flask import current_app

from .. import db
from ..mikrotik import RouterError, get_client
from ..models import Customer, InternetSession, Invoice, Router
from ..utils import utcnow
from .billing import charge_staff_subscriptions, generate_postpaid_invoices
from .core import audit, get_int, get_setting
from .provision import check_router, sync_customer
from .sms import notify


def expire_due_customers():
    n = 0
    now = utcnow()
    for c in Customer.query.filter(Customer.billing_type == "prepaid", Customer.status == "active",
                                   Customer.expires_at.isnot(None), Customer.expires_at <= now):
        c.status = "expired"
        sync_customer(c)  # disables the user and kicks the live session
        notify(c, "expired")
        n += 1
    db.session.commit()
    return n


def warn_expiring():
    days = get_int("expiry_warn_days", 3)
    if days <= 0:
        return 0
    now, n = utcnow(), 0
    for c in Customer.query.filter(Customer.billing_type == "prepaid", Customer.status == "active",
                                   Customer.expires_at.isnot(None), Customer.expires_at > now,
                                   Customer.expires_at <= now + timedelta(days=days),
                                   Customer.expiry_notified.is_(None)):
        notify(c, "expiry_warning")
        c.expiry_notified = now
        n += 1
    db.session.commit()
    return n


def suspend_overdue():
    grace = timedelta(days=get_int("grace_days", 3))
    now, n = utcnow(), 0
    ids = {i.customer_id for i in Invoice.query.filter(Invoice.status == "unpaid", Invoice.kind == "monthly",
                                                      Invoice.due_date + grace < now)}
    for c in Customer.query.filter(Customer.id.in_(ids or [0]), Customer.status == "active"):
        c.status = "suspended"
        sync_customer(c)
        notify(c, "suspended")
        n += 1
    db.session.commit()
    return n


def retry_failed_syncs():
    n = 0
    for c in Customer.query.filter_by(needs_sync=True):
        if sync_customer(c):
            n += 1
    db.session.commit()
    return n


def sync_sessions():
    """Poll routers: record login history, close ended sessions, enforce IP/MAC binding."""
    app = current_app._get_current_object()
    now = utcnow()
    for r in Router.query.filter_by(active=True):
        seen = set()
        try:
            with get_client(app, r) as c:
                live = [(k, row) for k in ("pppoe", "hotspot") for row in c.active(k)]
                for kind, row in live:
                    cust = Customer.query.filter_by(username=row["user"]).first()
                    if not cust:
                        continue
                    key = (cust.id, r.id)
                    seen.add(key)
                    if cust.bound_mac and row["mac"] and row["mac"].upper() != cust.bound_mac:
                        c.kick(kind, row["user"])
                        audit("mac_mismatch", f"{cust.username} from {row['mac']} on {r.name}", level="security")
                        continue
                    s = InternetSession.query.filter_by(customer_id=cust.id, router_id=r.id, ended_at=None).first()
                    if not s:
                        s = InternetSession(customer_id=cust.id, router_id=r.id, username=cust.username, kind=kind,
                                            ip=row["ip"], mac=row["mac"])
                        db.session.add(s)
                        if cust.router_id != r.id:
                            audit("roaming_login", f"{cust.username} roamed to {r.name}")
                    s.last_seen, s.bytes_in, s.bytes_out = now, row["bytes_in"], row["bytes_out"]
                    # users still online after expiry must be removed (router may have been unreachable)
                    if cust.status != "active":
                        c.kick(kind, row["user"])
            r.last_ok, r.last_error = True, ""
        except RouterError as e:
            r.last_ok, r.last_error = False, str(e)[:250]
            continue
        finally:
            r.last_checked = now
        for s in InternetSession.query.filter_by(router_id=r.id, ended_at=None):
            if (s.customer_id, r.id) not in seen:
                s.ended_at = now
    db.session.commit()


def run_all():
    """One automation tick."""
    out = {}
    out["expired"] = expire_due_customers()
    out["warned"] = warn_expiring()
    day, month = utcnow() + timedelta(hours=current_app.config["TZ_OFFSET_HOURS"]), None
    if day.day >= get_int("bill_day", 1):
        out["invoices"] = len(generate_postpaid_invoices())
        out["subscriptions"] = charge_staff_subscriptions()
    out["suspended"] = suspend_overdue()
    out["resynced"] = retry_failed_syncs()
    sync_sessions()
    return out


def start_background(app):
    interval = app.config["AUTOMATION_INTERVAL"]
    if interval <= 0:
        return

    def loop():
        while True:
            time.sleep(interval)
            with app.app_context():
                try:
                    run_all()
                except Exception as e:  # keep the loop alive
                    app.logger.exception("automation failed: %s", e)
                    db.session.rollback()

    threading.Thread(target=loop, daemon=True, name="automation").start()
