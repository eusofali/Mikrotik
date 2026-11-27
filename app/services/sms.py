"""SMS gateway with custom, event-driven templates."""
import urllib.parse
import urllib.request

from .. import db
from ..models import SmsLog, SmsTemplate
from ..utils import fmt_date, norm_phone
from .core import get_setting

DEFAULT_TEMPLATES = {
    "welcome": "Dear {name}, your {company} account is created. User: {username} Pass: {password}",
    "payment": "Dear {name}, payment of {amount} received (TrxID {trx}). Package {package} valid until {expiry}. Thanks, {company}",
    "activation": "Dear {name}, your {package} package is active until {expiry}. - {company}",
    "expiry_warning": "Dear {name}, your internet expires on {expiry}. Recharge now to stay online. - {company}",
    "expired": "Dear {name}, your internet package expired on {expiry}. Please recharge. - {company}",
    "invoice": "Dear {name}, invoice {invoice} of {amount} is due on {due}. - {company}",
    "suspended": "Dear {name}, your account has been suspended due to unpaid dues. Please pay to restore service. - {company}",
    "login": "Dear {name}, new login detected on your account from {ip}. - {company}",
    "ticket_reply": "Dear {name}, we replied to your support ticket #{ticket}. - {company}",
}


def seed_templates():
    for ev, body in DEFAULT_TEMPLATES.items():
        if not db.session.get(SmsTemplate, ev):
            db.session.add(SmsTemplate(event=ev, body=body))
    db.session.commit()


def render(body, **ctx):
    class D(dict):
        def __missing__(self, k):
            return ""
    return body.format_map(D(ctx))


def _transport(to, message):
    if get_setting("sms_mode") != "http" or not get_setting("sms_url"):
        return "logged", "log mode"
    params = get_setting("sms_params").replace("{to}", urllib.parse.quote(to)).replace(
        "{message}", urllib.parse.quote(message))
    url, method = get_setting("sms_url"), get_setting("sms_method").upper()
    try:
        if method == "POST":
            req = urllib.request.Request(url, data=params.encode(), method="POST")
        else:
            req = urllib.request.Request(url + ("&" if "?" in url else "?") + params)
        with urllib.request.urlopen(req, timeout=10) as r:
            return "sent", r.read(200).decode("utf-8", "replace")
    except Exception as e:  # network/gateway failure must never break billing flow
        return "failed", str(e)[:200]


def send_sms(to, message, event="", customer_id=None):
    to = norm_phone(to)
    if not to:
        return None
    status, detail = _transport(to, message)
    log = SmsLog(to=to, message=message, event=event, status=status, detail=detail, customer_id=customer_id)
    db.session.add(log)
    return log


def notify(customer, event, **extra):
    """Send the template for `event` to a customer (if enabled)."""
    tpl = db.session.get(SmsTemplate, event)
    if not tpl or not tpl.enabled or not customer.phone:
        return None
    ctx = dict(
        name=customer.name, username=customer.username, company=get_setting("company_name"),
        package=customer.package.name if customer.package else "", expiry=fmt_date(customer.expires_at),
    )
    ctx.update(extra)
    return send_sms(customer.phone, render(tpl.body, **ctx), event, customer.id)
