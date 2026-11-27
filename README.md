# ISP Billing & Automation (MikroTik PPPoE + Hotspot)

MikroTik PPPoE ও Hotspot ভিত্তিক ISP-র জন্য বিলিং, ইউজার ম্যানেজমেন্ট ও অটোমেশন সফটওয়্যার (Flask + SQLAlchemy)।

## Quick start / দ্রুত চালু করুন
```bash
pip install -r requirements.txt
export MIKROTIK_MOCK=1                 # simulated routers for a demo; unset for real routers
flask --app wsgi seed-demo             # demo data
flask --app wsgi run                   # http://localhost:5000
```
| Panel | URL | Demo login |
|---|---|---|
| Admin / Distributor / Reseller | `/staff/login` | `admin/admin123`, `dist1/dist123`, `res1/res123` |
| Customer portal | `/portal/login` | `user1 / pass1234` |
| Branded reseller portal | `/r/<slug>` (e.g. `/r/karim`) | same customers |

Without `seed-demo` an `admin` user is created from `ADMIN_USER` / `ADMIN_PASSWORD` (default `admin123` — **change it**).
Run tests: `pytest`. Production: see `.env.example`, `Dockerfile`, `docs/mikrotik-setup.md`.

## Features → where
| Feature | Implementation |
|---|---|
| PPPoE/Hotspot automation, auto expiry/disconnect, suspend/delete/activate | `services/provision.py`, `services/automation.py`, customer page actions |
| Real RouterOS API (8728/8729) + simulator | `mikrotik.py` |
| Multi-router, central control, live CPU/RAM/traffic, online users | Routers, Dashboard, Live monitor (`/api/router/<id>/live`) |
| Roaming (hotspot) | roaming-enabled routers get every roaming customer; roaming logins logged |
| IP/MAC binding, unauthorized access control | customer page → binding; MAC mismatch kicked + security log; login throttling, CSRF, role scoping, cookie flags, audit log |
| Payments | bKash tokenized checkout, SSLCommerz, Send-Money (bKash/Nagad/Rocket/Upay) with SMS auto-verification (`POST /api/sms-webhook`), built-in sandbox gateway |
| Self-service portal | buy package, payment/internet/bill history, invoice PDF, password/profile, tickets |
| Billing | auto invoices (postpaid), invoice PDF/print, daily/monthly income, dues |
| Reseller/Distributor | hierarchy, white-label branding, top-up wallet, prepaid/postpaid + credit limit, commission or per-user/flat subscription, ledger |
| SMS | HTTP gateway (any provider via URL template), editable event templates, bulk SMS |
| Support tickets | customer ↔ staff threads, SMS on reply |
| Reports | income / users / payments, CSV export |

## Send-Money auto activation
1. Settings → set your bKash/Nagad/Rocket/Upay receiving numbers and press *Generate SMS-webhook token*.
2. On the merchant phone, use an SMS-forwarder app to POST every incoming message to
   `https://<host>/api/sms-webhook` with header `X-Token: <token>` and JSON `{"message": "<sms text>"}`.
3. Customer sends money, enters the TrxID → package activates automatically when amount and TrxID match
   (under-payments go to manual review; each TrxID can be used once).

## Honest limitations
* Router/gateway code is tested against a simulator and a protocol-level fake RouterOS server, **not** against live
  hardware or live bKash/SSLCommerz accounts — test in sandbox first. Nagad/Rocket/Upay merchant APIs are not
  integrated (they use Send-Money + SMS verification above).
* Money is stored as floats rounded to 2 decimals; migrate to `Numeric` if you need strict accounting.
* No RADIUS server: users are written directly to the routers (fine for tens of thousands of users per router, not for
  per-packet accounting). Usage history comes from periodic polling.
* Run a single worker (the automation loop is in-process) or use `AUTOMATION_INTERVAL=0` plus cron.
