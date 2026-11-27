import pytest

from app import create_app, db
from app.config import TestConfig
from app.mikrotik import reset_mock
from app.models import Customer, Package, Router, Staff


@pytest.fixture
def app():
    reset_mock()
    app = create_app(TestConfig)
    with app.app_context():
        r1 = Router(name="R1", host="h1", mock=True, zone="A")
        r2 = Router(name="R2", host="h2", mock=True, zone="B")
        db.session.add_all([r1, r2])
        db.session.add_all([
            Package(name="Home 10M", kind="pppoe", price=500, validity_days=30, rate_limit="10M/10M"),
            Package(name="Hot 1D", kind="hotspot", price=20, validity_days=1, rate_limit="2M/2M"),
        ])
        db.session.commit()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def csrf(client, path="/staff/login"):
    import re
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def login_staff(client, user="admin", pw="admin123"):
    t = csrf(client)
    return client.post("/staff/login", data=dict(username=user, password=pw, csrf_token=t), follow_redirects=True)


def login_customer(client, user, pw):
    t = csrf(client, "/portal/login")
    return client.post("/portal/login", data=dict(username=user, password=pw, csrf_token=t), follow_redirects=True)


def post(client, path, page, **data):
    """POST with a CSRF token scraped from `page`."""
    data["csrf_token"] = csrf(client, page)
    return client.post(path, data=data, follow_redirects=True)
