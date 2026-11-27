from datetime import timedelta

from app import db
from app.mikrotik import MockRouter
from app.models import (Customer, InternetSession, Invoice, LedgerEntry, Package, Payment, ReceivedTxn, Router,
                        SmsLog, Staff)
from app.services import automation, payments as pay
from app.services.billing import (BillingError, charge_staff_subscriptions, generate_postpaid_invoices, receive_payment,
                                  recharge_by_staff, topup)
from app.services.core import set_setting
from app.services.provision import sync_customer
from app.utils import utcnow

from .conftest import csrf, login_customer, login_staff, post


def mk_customer(username="u1", kind="pppoe", reseller=None, router_id=1, phone="01711111111"):
    c = Customer(username=username, service_password="pw123456", name="Test " + username, kind=kind,
                 router_id=router_id, reseller=reseller, phone=phone)
    c.set_portal_password("pw123456")
    db.session.add(c)
    db.session.flush()
    return c


def mk_staff(username, role, parent, **kw):
    s = Staff(username=username, name=username, role=role, parent=parent, **kw)
    s.set_password("x")
    db.session.add(s)
    db.session.flush()
    return s


def admin():
    return Staff.query.filter_by(role="admin").first()


def test_recharge_provisions_router_and_expires(app):
    c = mk_customer()
    pkg = Package.query.filter_by(kind="pppoe").first()
    recharge_by_staff(admin(), c, pkg)
    db.session.commit()
    assert c.status == "active" and c.expires_at > utcnow() + timedelta(days=29)
    r = MockRouter("r1")
    assert r.s["users"][("pppoe", "u1")]["disabled"] is False
    assert r.s["profiles"][("pppoe", pkg.profile_name)] == "10M/10M"
    assert Payment.query.filter_by(customer_id=c.id, status="success").count() == 1
    assert Invoice.query.first().status == "paid"
    assert SmsLog.query.filter_by(event="payment").count() == 1
    # go online, then let validity lapse -> auto disconnect
    r.simulate_login("pppoe", "u1")
    c.expires_at = utcnow() - timedelta(minutes=1)
    db.session.commit()
    assert automation.expire_due_customers() == 1
    assert c.status == "expired"
    assert r.s["users"][("pppoe", "u1")]["disabled"] is True
    assert ("pppoe", "u1") not in r.s["active"]
    assert SmsLog.query.filter_by(event="expired").count() == 1


def test_recharge_stacks_validity(app):
    c = mk_customer()
    pkg = Package.query.filter_by(kind="pppoe").first()
    recharge_by_staff(admin(), c, pkg)
    first = c.expires_at
    recharge_by_staff(admin(), c, pkg)
    assert c.expires_at == first + timedelta(days=30)


def test_reseller_prepaid_wallet_and_commission_chain(app):
    adm = admin()
    dist = mk_staff("d", "distributor", adm, commission_pct=20, payment_mode="postpaid", credit_limit=1000)
    res = mk_staff("r", "reseller", dist, commission_pct=10)
    c = mk_customer(reseller=res)
    pkg = Package.query.filter_by(kind="pppoe").first()
    with_raises = False
    try:
        recharge_by_staff(res, c, pkg)  # empty prepaid wallet
    except BillingError:
        with_raises = True
    assert with_raises
    topup(adm, dist, 0.0 + 1000)
    topup(dist, res, 450)
    recharge_by_staff(res, c, pkg)  # price 500, reseller pays 450
    db.session.commit()
    assert res.balance == 0
    # distributor: -450 for topup, +450 recharge share... earns 450-400=50 commission
    ledger_kinds = [e.kind for e in LedgerEntry.query.filter_by(staff_id=dist.id)]
    assert "commission" in ledger_kinds
    p = Payment.query.filter_by(customer_id=c.id).first()
    assert p.amount == 500 and p.net_amount == 400  # ISP receives distributor's cost


def test_postpaid_credit_limit_and_dues(app):
    adm = admin()
    dist = mk_staff("d", "distributor", adm, payment_mode="postpaid", credit_limit=600, commission_pct=0)
    c = mk_customer(reseller=dist)
    pkg = Package.query.filter_by(kind="pppoe").first()
    recharge_by_staff(dist, c, pkg)  # -500 (within limit)
    assert dist.balance == -500 and dist.due == 500
    try:
        recharge_by_staff(dist, c, pkg)
        assert False
    except BillingError:
        pass
    receive_payment(adm, dist, 500)
    assert dist.balance == 0


def test_per_user_billing_model(app):
    adm = admin()
    res = mk_staff("r", "reseller", adm, billing_model="per_user", per_user_fee=10, monthly_fee=100)
    c = mk_customer(reseller=res)
    pkg = Package.query.filter_by(kind="pppoe").first()
    recharge_by_staff(res, c, pkg)
    assert res.balance == 0  # recharge is free for per-user model
    assert charge_staff_subscriptions() == 1
    assert res.balance == -110
    assert charge_staff_subscriptions() == 0  # once per month


def test_postpaid_customer_invoice_suspend_and_restore(app):
    c = mk_customer()
    c.billing_type, c.status = "postpaid", "active"
    c.package = Package.query.filter_by(kind="pppoe").first()
    db.session.commit()
    inv = generate_postpaid_invoices()
    assert len(inv) == 1 and inv[0].amount == 500
    assert generate_postpaid_invoices() == []  # idempotent per period
    inv[0].due_date = utcnow() - timedelta(days=10)
    db.session.commit()
    assert automation.suspend_overdue() == 1
    assert c.status == "suspended"
    p = pay.start(c, inv[0], "sandbox")[2]
    pay.finalize(p, "T1")
    db.session.commit()
    assert c.status == "active" and inv[0].status == "paid"


def test_hotspot_roaming_provisions_all_roaming_routers(app):
    c = mk_customer("h1", kind="hotspot")
    c.package = Package.query.filter_by(kind="hotspot").first()
    c.status = "active"
    sync_customer(c)
    assert ("hotspot", "h1") in MockRouter("r1").s["users"]
    assert ("hotspot", "h1") in MockRouter("r2").s["users"]
    c.roaming = False
    sync_customer(c)
    c2 = mk_customer("p1", kind="pppoe")
    sync_customer(c2)
    assert ("pppoe", "p1") not in MockRouter("r2").s["users"]


def test_session_sync_history_roaming_and_mac_binding(app):
    c = mk_customer("h1", kind="hotspot")
    c.package, c.status = Package.query.filter_by(kind="hotspot").first(), "active"
    c.bound_mac = "AA:BB:CC:00:00:01"
    sync_customer(c)
    db.session.commit()
    MockRouter("r2").simulate_login("hotspot", "h1", mac="AA:BB:CC:00:00:01")
    automation.sync_sessions()
    s = InternetSession.query.filter_by(customer_id=c.id).all()
    assert len(s) == 1 and s[0].router_id == 2 and s[0].ended_at is None
    # a different device using the same credentials is kicked and logged
    MockRouter("r1").simulate_login("hotspot", "h1", mac="DE:AD:BE:EF:00:01")
    automation.sync_sessions()
    assert ("hotspot", "h1") not in MockRouter("r1").s["active"]
    from app.models import AuditLog
    assert AuditLog.query.filter_by(action="mac_mismatch").count() == 1
    # logout on r2 closes the history row
    MockRouter("r2").s["active"].clear()
    automation.sync_sessions()
    assert InternetSession.query.filter_by(customer_id=c.id).first().ended_at is not None


def test_router_failure_marks_needs_sync_and_retry(app):
    c = mk_customer()
    c.package, c.status = Package.query.filter_by(kind="pppoe").first(), "active"
    from unittest import mock
    from app.mikrotik import RouterError
    with mock.patch.object(MockRouter, "upsert_user", side_effect=RouterError("down")):
        assert sync_customer(c) is False
    assert c.needs_sync and "down" in c.sync_error
    assert automation.retry_failed_syncs() == 1
    assert not c.needs_sync


def test_sms_parsing_and_send_money_auto_activation(app):
    c = mk_customer()
    pkg = Package.query.filter_by(kind="pppoe").first()
    from app.services.billing import create_invoice
    inv = create_invoice(c, 500, "package", pkg, due_days=0)
    set_setting("bkash_number", "01700000000")
    p = pay.start(c, inv, "manual_bkash")[2]
    # SMS arrives first
    sms = "You have received Tk 500.00 from 01712345678. Fee Tk 0.00. Balance Tk 1,500.00. TrxID 8N7A6D5E4C at 12/11/2025 10:30"
    d = pay.parse_sms(sms)
    assert d["trx_id"] == "8N7A6D5E4C" and d["amount"] == 500
    assert d["sender"] == "01712345678"
    pay.ingest_sms(sms)
    pay.submit_manual(p, "8n7a6d5e4c", "01712345678")
    db.session.commit()
    assert p.status == "success" and c.status == "active"
    # TrxID cannot be reused
    inv2 = create_invoice(c, 500, "package", pkg, due_days=0)
    p2 = pay.start(c, inv2, "manual_bkash")[2]
    try:
        pay.submit_manual(p2, "8N7A6D5E4C")
        assert False
    except pay.PaymentError:
        pass


def test_send_money_customer_first_then_sms_and_underpay(app):
    c = mk_customer()
    pkg = Package.query.filter_by(kind="pppoe").first()
    from app.services.billing import create_invoice
    inv = create_invoice(c, 500, "package", pkg, due_days=0)
    p = pay.start(c, inv, "manual_nagad")[2]
    pay.submit_manual(p, "NG123456", "01711111111")
    assert p.status == "review"
    pay.ingest_sms("Money Received. Amount: Tk 300.00 Sender: 01711111111 Ref: N/A TxnID: NG123456 Balance: Tk 900")
    assert p.status == "review" and "mismatch" in p.note  # underpaid -> manual review, not activated
    assert c.status != "active"
    from app.models import Staff
    pay.approve_manually(p, admin())
    assert p.status == "success" and c.status == "active"


def test_reseller_online_payment_credits_commission(app):
    adm = admin()
    res = mk_staff("r", "reseller", adm, commission_pct=10)
    c = mk_customer(reseller=res)
    pkg = Package.query.filter_by(kind="pppoe").first()
    from app.services.billing import create_invoice
    inv = create_invoice(c, 500, "package", pkg, due_days=0)
    p = pay.start(c, inv, "sandbox")[2]
    pay.finalize(p, "SBX1")
    assert res.balance == 50 and p.net_amount == 450


# ------------------------------------------------------------------ HTTP layer
def test_csrf_and_login_throttle(app, client):
    assert client.post("/staff/login", data=dict(username="admin", password="admin123")).status_code == 400
    for _ in range(5):
        t = csrf(client)
        client.post("/staff/login", data=dict(username="admin", password="bad", csrf_token=t))
    t = csrf(client)
    r = client.post("/staff/login", data=dict(username="admin", password="admin123", csrf_token=t))
    assert r.status_code == 429  # locked even with the right password


def test_role_isolation(app, client):
    adm = admin()
    d1 = mk_staff("d1", "distributor", adm)
    d2 = mk_staff("d2", "distributor", adm)
    r1 = mk_staff("r1", "reseller", d1)
    c_other = mk_customer("other", reseller=d2)
    c_mine = mk_customer("mine", reseller=r1)
    d1.set_password("pw"); db.session.commit()
    cid_other, cid_mine = c_other.id, c_mine.id
    login_staff(client, "d1", "pw")
    assert client.get(f"/staff/customers/{cid_mine}").status_code == 200
    assert client.get(f"/staff/customers/{cid_other}").status_code == 404
    assert client.get("/staff/settings").status_code == 403
    assert client.get("/staff/routers").status_code == 403
    assert client.get("/api/router/1/live").status_code == 403


def test_admin_pages_render(app, client):
    mk_customer()
    db.session.commit()
    login_staff(client)
    for path in ["/staff/", "/staff/customers", "/staff/customers/new", "/staff/customers/1", "/staff/packages",
                 "/staff/packages/new", "/staff/routers", "/staff/routers/new", "/staff/monitor", "/staff/team",
                 "/staff/team/new", "/staff/wallet", "/staff/invoices", "/staff/payments", "/staff/tickets",
                 "/staff/sms", "/staff/reports/income", "/staff/reports/users", "/staff/reports/payments",
                 "/staff/settings", "/staff/security", "/api/router/1/live"]:
        r = client.get(path)
        assert r.status_code == 200, path
    assert client.get("/staff/reports/users?format=csv").mimetype == "text/csv"


def test_staff_creates_customer_via_ui_and_recharge(app, client):
    login_staff(client)
    pkg = Package.query.filter_by(kind="pppoe").first()
    r = post(client, "/staff/customers/new", "/staff/customers/new", name="Ali", phone="+8801799999999",
             username="ali", password="secret12", kind="pppoe", router_id="1", billing_type="prepaid",
             package_id=str(pkg.id), activate="1", monthly_charge="0")
    assert b"created" in r.data
    c = Customer.query.filter_by(username="ali").first()
    assert c.status == "active" and c.phone == "01799999999"
    r = post(client, f"/staff/customers/{c.id}", f"/staff/customers/{c.id}", action="suspend")
    assert Customer.query.get(c.id).status == "suspended"
    assert MockRouter("r1").s["users"][("pppoe", "ali")]["disabled"] is True
    post(client, f"/staff/customers/{c.id}", f"/staff/customers/{c.id}", action="binding", mac="aa-bb-cc-dd-ee-ff")
    assert Customer.query.get(c.id).bound_mac == "AA:BB:CC:DD:EE:FF"
    post(client, f"/staff/customers/{c.id}", f"/staff/customers/{c.id}", action="delete")
    assert ("pppoe", "ali") not in MockRouter("r1").s["users"]
    assert Customer.query.get(c.id).status == "deleted"


def test_customer_portal_purchase_with_sandbox_and_history(app, client):
    c = mk_customer("cust")
    db.session.commit()
    login_customer(client, "cust", "pw123456")
    pkg = Package.query.filter_by(kind="pppoe").first()
    r = client.post(f"/portal/buy/{pkg.id}", data=dict(method="sandbox", csrf_token=csrf(client, "/portal/packages")))
    assert r.status_code == 302 and "/pay/sandbox/" in r.location
    r = client.post(r.location, data=dict(result="pay", csrf_token=csrf(client, "/portal/")), follow_redirects=True)
    assert Customer.query.filter_by(username="cust").first().status == "active"
    assert client.get("/portal/history").status_code == 200
    inv = Invoice.query.first()
    pdf = client.get(f"/portal/invoice/{inv.id}.pdf")
    assert pdf.data.startswith(b"%PDF") and pdf.mimetype == "application/pdf"


def test_customer_password_change_syncs_router(app, client):
    c = mk_customer("cust")
    c.package, c.status = Package.query.filter_by(kind="pppoe").first(), "active"
    sync_customer(c)
    db.session.commit()
    login_customer(client, "cust", "pw123456")
    post(client, "/portal/profile", "/portal/profile", action="password", current="pw123456", new="newpass1",
         confirm="newpass1")
    assert MockRouter("r1").s["users"][("pppoe", "cust")]["password"] == "newpass1"
    assert Customer.query.filter_by(username="cust").first().check_portal_password("newpass1")


def test_ticket_flow_and_branding(app, client):
    adm = admin()
    res = mk_staff("r1", "reseller", adm, brand_name="Karim Net", brand_slug="karim")
    c = mk_customer("cust", reseller=res)
    db.session.commit()
    assert b"Karim Net" in client.get("/r/karim").data
    login_customer(client, "cust", "pw123456")
    post(client, "/portal/tickets", "/portal/tickets", subject="No internet", body="Please help", category="technical")
    from app.models import Ticket
    t = Ticket.query.first()
    assert t.status == "open"
    client.post("/portal/logout", data=dict(csrf_token=csrf(client, "/portal/")))
    res.set_password("pw"); db.session.commit()
    login_staff(client, "r1", "pw")
    post(client, f"/staff/tickets/{t.id}", f"/staff/tickets/{t.id}", body="Fixed", status="closed")
    t = Ticket.query.first()
    assert t.status == "closed" and len(t.messages) == 2
    assert SmsLog.query.filter_by(event="ticket_reply").count() == 1


def test_sms_webhook_auth_and_activation(app, client):
    set_setting("sms_webhook_token", "tok123")
    c = mk_customer()
    pkg = Package.query.filter_by(kind="pppoe").first()
    from app.services.billing import create_invoice
    inv = create_invoice(c, 500, "package", pkg, due_days=0)
    set_setting("bkash_number", "01700000000")
    p = pay.start(c, inv, "manual_bkash")[2]
    pay.submit_manual(p, "WEBHOOK01")
    db.session.commit()
    pid = p.id
    assert client.post("/api/sms-webhook", json={"message": "x"}).status_code == 401
    r = client.post("/api/sms-webhook", headers={"X-Token": "tok123"},
                    json={"message": "You have received Tk 500 from 01711111111. TrxID WEBHOOK01 at 10:00"})
    assert r.get_json() == {"ok": True, "activated": True}
    assert db.session.get(Payment, pid).status == "success"
