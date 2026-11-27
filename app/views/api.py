"""JSON endpoints for the live dashboard / monitoring."""
from flask import Blueprint, current_app, g, jsonify

from .. import db
from ..mikrotik import RouterError, get_client
from ..models import Customer, InternetSession, Router
from .guards import scoped_customers, staff_required

bp = Blueprint("api", __name__, url_prefix="/api")


@bp.route("/router/<int:rid>/live")
@staff_required("admin")
def router_live(rid):
    r = db.get_or_404(Router, rid)
    try:
        with get_client(current_app._get_current_object(), r) as c:
            res = c.resources()
            traffic = c.interfaces_traffic()
            pppoe, hotspot = c.active("pppoe"), c.active("hotspot")
        return jsonify(ok=True, resources=res, traffic=traffic, pppoe=len(pppoe), hotspot=len(hotspot))
    except RouterError as e:
        return jsonify(ok=False, error=str(e)), 502


@bp.route("/online")
@staff_required()
def online():
    ids = scoped_customers(g.staff).with_entities(Customer.id)
    n = InternetSession.query.filter(InternetSession.ended_at.is_(None), InternetSession.customer_id.in_(ids)).count()
    return jsonify(online=n)
