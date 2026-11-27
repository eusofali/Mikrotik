"""Settings, audit log and login-throttle helpers."""
from datetime import timedelta

from flask import current_app, request

from .. import db
from ..models import AuditLog, LoginAttempt, Setting
from ..utils import utcnow

DEFAULT_SETTINGS = {
    "company_name": "My ISP",
    "company_phone": "",
    "company_address": "",
    "currency": "৳",
    "grace_days": "3",  # postpaid: days after due date before suspension
    "invoice_due_days": "7",
    "bill_day": "1",  # postpaid monthly bill generation day
    "expiry_warn_days": "3",
    # send-money receiving numbers shown to customers
    "bkash_number": "", "nagad_number": "", "rocket_number": "", "upay_number": "",
    "sms_webhook_token": "",
    # SMS gateway: generic HTTP GET/POST
    "sms_mode": "log",  # log | http
    "sms_url": "", "sms_method": "GET", "sms_params": "api_key=KEY&senderid=ID&number={to}&message={message}",
    # payment gateways
    "bkash_enabled": "0", "bkash_base": "https://tokenized.sandbox.bka.sh/v1.2.0-beta",
    "bkash_app_key": "", "bkash_app_secret": "", "bkash_username": "", "bkash_password": "",
    "sslcommerz_enabled": "0", "sslcommerz_sandbox": "1", "sslcommerz_store_id": "", "sslcommerz_store_pass": "",
    "sandbox_enabled": "1",  # built-in fake gateway for testing
    "manual_enabled": "1",
}


def get_setting(key, default=None):
    row = db.session.get(Setting, key)
    if row is not None:
        return row.value
    return DEFAULT_SETTINGS.get(key, default if default is not None else "")


def set_setting(key, value):
    row = db.session.get(Setting, key)
    if row:
        row.value = value
    else:
        db.session.add(Setting(key=key, value=value))


def get_int(key, default=0):
    try:
        return int(get_setting(key))
    except (TypeError, ValueError):
        return default


def audit(action, detail="", actor="", level="info"):
    try:
        ip = request.remote_addr if request else ""
    except RuntimeError:
        ip = ""
    db.session.add(AuditLog(action=action, detail=str(detail)[:500], actor=actor, level=level, ip=ip or ""))


def _key(scope, ident):
    return f"{scope}:{ident}:{request.remote_addr}"[:160]


def login_locked(scope, ident):
    row = LoginAttempt.query.filter_by(key=_key(scope, ident)).first()
    return bool(row and row.locked_until and row.locked_until > utcnow())


def login_failed(scope, ident):
    cfg = current_app.config
    row = LoginAttempt.query.filter_by(key=_key(scope, ident)).first()
    if not row:
        row = LoginAttempt(key=_key(scope, ident), fails=0)
        db.session.add(row)
    row.fails = (row.fails or 0) + 1
    row.updated_at = utcnow()
    if row.fails >= cfg["MAX_LOGIN_FAILS"]:
        row.locked_until = utcnow() + timedelta(minutes=cfg["LOGIN_LOCK_MINUTES"])
        row.fails = 0
        audit("login_locked", f"{scope}:{ident}", level="security")
    db.session.commit()


def login_ok(scope, ident):
    LoginAttempt.query.filter_by(key=_key(scope, ident)).delete()
    db.session.commit()
