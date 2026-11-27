from flask import Blueprint, flash, g, redirect, render_template, request, session, url_for

from .. import db
from ..models import Customer, Staff
from ..services.core import audit, login_failed, login_locked, login_ok
from .guards import load_actor

bp = Blueprint("auth", __name__)


def _safe_next(default):
    n = request.args.get("next", "")
    return n if n.startswith("/") and not n.startswith("//") else default


@bp.route("/staff/login", methods=["GET", "POST"])
def staff_login():
    if request.method == "POST":
        u = request.form.get("username", "").strip()
        if login_locked("staff", u):
            flash("Too many failed attempts. Try again later.", "error")
            return render_template("login.html", kind="staff"), 429
        s = Staff.query.filter_by(username=u).first()
        if s and s.active and s.check_password(request.form.get("password", "")):
            login_ok("staff", u)
            session.clear()
            session.permanent = True
            session["staff_id"] = s.id
            audit("staff_login", u, actor=u)
            db.session.commit()
            return redirect(_safe_next(url_for("staff.dashboard")))
        login_failed("staff", u)
        audit("staff_login_failed", u, level="security")
        db.session.commit()
        flash("Invalid username or password", "error")
    return render_template("login.html", kind="staff")


@bp.route("/staff/logout", methods=["POST"])
def staff_logout():
    session.clear()
    return redirect(url_for("auth.staff_login"))
