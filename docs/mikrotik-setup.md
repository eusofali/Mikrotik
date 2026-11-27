# MikroTik setup

## 1. API user (least privilege)
```
/user group add name=billing policy=api,read,write,test,policy,ppp,sensitive comment="ISP billing"
/user add name=billing group=billing password=STRONG_PASSWORD address=<BILLING_SERVER_IP>
/ip service set api disabled=no address=<BILLING_SERVER_IP>/32      ;# plain API 8728
# or, recommended over untrusted links:
/ip service set api-ssl disabled=no address=<BILLING_SERVER_IP>/32  ;# 8729 (needs a certificate)
```
Add the router in **Routers → Add router** (host, port, user, password) and press **Test**.

## 2. PPPoE
```
/ppp aaa set use-radius=no
/interface pppoe-server server add service-name=isp interface=<LAN> default-profile=default disabled=no
```
Profiles (`pkg-<id>` or the name you set on the package) and secrets are created/updated/disabled automatically.
Expired users are disabled **and** their active session is removed (auto-disconnect).
A MAC bound in the billing panel is written to the secret's `caller-id`.

## 3. Hotspot
```
/ip hotspot setup            ;# create the hotspot on your LAN bridge
/ip hotspot profile set default login-by=http-chap,cookie,http-pap
/ip hotspot profile set default http-cookie-lifetime=1d   ;# cookie = auto re-login
```
Hotspot users, limits (`rate-limit`, `limit-bytes-total`) and **IP bindings** (`type=regular` for a bound MAC) are managed
automatically. Users whose MAC does not match their binding are kicked and logged under **Security log**.

Optional auto-login page: edit `hotspot/login.html` on the router so customers land on the portal
(`https://<billing>/portal/login`) to buy a package, then return to the router login with the same credentials.

## 4. Roaming
Mark each hotspot router *Allow hotspot roaming*. Every roaming-enabled customer is provisioned on all of them, so a user
who recharged in one zone can log in in any other zone. Roaming logins appear in the audit log and the user's history.

## 5. Automation
The background loop runs inside the app when `AUTOMATION_INTERVAL` (seconds) is set. Alternative: cron
```
* * * * *  cd /opt/isp && flask --app wsgi automation
```
Each tick: expire due users, warn before expiry, generate postpaid invoices, suspend overdue, retry failed router syncs,
poll routers for login history and enforce MAC binding.
