import os
import random

import click

from . import db
from .models import Customer, Package, Router, Staff
from .services import automation


def ensure_admin():
    if not Staff.query.filter_by(role="admin").first():
        pw = os.environ.get("ADMIN_PASSWORD", "admin123")
        a = Staff(username=os.environ.get("ADMIN_USER", "admin"), name="Administrator", role="admin")
        a.set_password(pw)
        db.session.add(a)
        db.session.commit()


def register_cli(app):
    @app.cli.command("automation")
    def automation_cmd():
        """Run one automation tick (expiry, billing, sync). Put it in cron: * * * * *"""
        click.echo(automation.run_all())

    @app.cli.command("seed-demo")
    def seed_demo():
        """Create demo routers, packages, resellers and customers (uses simulated routers)."""
        from .services.billing import activate_package, topup
        from .services.provision import sync_customer

        if Router.query.first():
            click.echo("already seeded")
            return
        r1 = Router(name="Dhaka-Core", host="demo1", zone="Dhaka", mock=True, allow_roaming=True)
        r2 = Router(name="Chattogram-Hotspot", host="demo2", zone="Chattogram", mock=True, allow_roaming=True)
        db.session.add_all([r1, r2])
        pk = [Package(name="Home 5M", kind="pppoe", price=500, validity_days=30, rate_limit="5M/5M"),
              Package(name="Home 10M", kind="pppoe", price=800, validity_days=30, rate_limit="10M/10M"),
              Package(name="Hotspot 1 Day", kind="hotspot", price=20, validity_days=1, rate_limit="2M/2M"),
              Package(name="Hotspot 30 Days", kind="hotspot", price=300, validity_days=30, rate_limit="4M/4M")]
        db.session.add_all(pk)
        admin = Staff.query.filter_by(role="admin").first()
        dist = Staff(username="dist1", name="Rahim Distributor", role="distributor", parent=admin,
                     payment_mode="postpaid", credit_limit=20000, commission_pct=15, brand_name="Rahim Net",
                     brand_slug="rahim")
        dist.set_password("dist123")
        db.session.add(dist)
        db.session.flush()
        res = Staff(username="res1", name="Karim Reseller", role="reseller", parent=dist, commission_pct=10,
                    brand_name="Karim Broadband", brand_slug="karim")
        res.set_password("res123")
        db.session.add(res)
        db.session.flush()
        topup(admin, dist, 10000)
        topup(dist, res, 3000)
        for i in range(1, 6):
            c = Customer(username=f"user{i}", service_password="pass1234", name=f"Customer {i}",
                         phone=f"017000000{i:02d}", kind="pppoe" if i < 4 else "hotspot", router=r1, reseller=res)
            c.set_portal_password("pass1234")
            db.session.add(c)
            db.session.flush()
            activate_package(c, pk[random.choice([0, 1])] if i < 4 else pk[3], notify_sms=False)
        db.session.commit()
        click.echo("seeded. admin/admin123  dist1/dist123  res1/res123  customers user1..5 / pass1234")

    @app.cli.command("create-admin")
    @click.argument("username")
    @click.argument("password")
    def create_admin(username, password):
        s = Staff.query.filter_by(username=username).first() or Staff(username=username, name=username, role="admin")
        s.set_password(password)
        db.session.add(s)
        db.session.commit()
        click.echo("ok")
