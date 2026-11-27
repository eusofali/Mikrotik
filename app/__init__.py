import secrets
from datetime import timedelta

from flask import Flask, abort, g, redirect, request, session, url_for
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
CSRF_EXEMPT = set()


def csrf_exempt(fn):
    CSRF_EXEMPT.add(f"{fn.__module__}.{fn.__name__}")
    return fn


def create_app(config=None):
    from .config import Config

    app = Flask(__name__)
    app.config.from_object(config or Config)
    app.permanent_session_lifetime = timedelta(minutes=app.config["PERMANENT_SESSION_LIFETIME_MIN"])
    if app.config.get("TRUST_PROXY"):  # behind nginx: use X-Forwarded-For/Proto for real client IP
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    db.init_app(app)

    from . import models  # noqa: F401
    from .views import api, auth, pay, portal, staff

    for bp in (auth.bp, staff.bp, portal.bp, portal.brand_login_bp, pay.bp, api.bp):
        app.register_blueprint(bp)

    from .utils import fmt_date, fmt_dt, human_bytes
    from .services.core import get_setting

    app.jinja_env.filters.update(dt=fmt_dt, date=fmt_date, bytes=human_bytes)

    @app.before_request
    def _csrf():
        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            view = app.view_functions.get(request.endpoint)
            if view and f"{view.__module__}.{view.__name__}" in CSRF_EXEMPT:
                return
            tok = session.get("_csrf")
            sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
            if not tok or not sent or not secrets.compare_digest(tok, sent):
                abort(400, "CSRF token missing or invalid")

    def csrf_token():
        if "_csrf" not in session:
            session["_csrf"] = secrets.token_hex(16)
        return session["_csrf"]

    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.context_processor
    def _ctx():
        return dict(company=get_setting("company_name"),
                    currency=get_setting("currency"), me=getattr(g, "staff", None) or getattr(g, "customer", None))

    @app.after_request
    def _headers(resp):
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        return resp

    @app.route("/")
    def index():
        return redirect(url_for("portal.login"))

    @app.errorhandler(403)
    def _403(e):
        return "Forbidden", 403

    from .cli import register_cli
    register_cli(app)

    with app.app_context():
        db.create_all()
        from .services.sms import seed_templates
        seed_templates()
        from .cli import ensure_admin
        ensure_admin()

    from .services.automation import start_background
    if not app.config.get("TESTING"):
        start_background(app)
    return app
