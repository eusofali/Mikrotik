"""MikroTik RouterOS integration.

`RouterOSClient` speaks the real RouterOS API protocol (TCP 8728 / TLS 8729) with
no third-party dependency.  `MockRouter` is an in-memory simulation with the same
high-level interface, used for development, demos and tests.
"""
import random
import socket
import ssl
import threading
import time


class RouterError(Exception):
    pass


# --------------------------------------------------------------------------- real client
class RouterOSClient:
    def __init__(self, host, port=8728, username="admin", password="", use_ssl=False, timeout=5.0,
                 hotspot_server=""):
        self.host, self.port = host, int(port or 8728)
        self.username, self.password = username, password
        self.use_ssl, self.timeout = use_ssl, timeout
        self.hotspot_server = hotspot_server
        self.sock = None

    # -- protocol ---------------------------------------------------------
    @staticmethod
    def _enc_len(n):
        if n < 0x80:
            return bytes([n])
        if n < 0x4000:
            return (n | 0x8000).to_bytes(2, "big")
        if n < 0x200000:
            return (n | 0xC00000).to_bytes(3, "big")
        if n < 0x10000000:
            return (n | 0xE0000000).to_bytes(4, "big")
        return b"\xf0" + n.to_bytes(4, "big")

    def _recv(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise RouterError("connection closed by router")
            buf += chunk
        return buf

    def _read_len(self):
        c = self._recv(1)[0]
        if c & 0x80 == 0:
            return c
        if c & 0xC0 == 0x80:
            return ((c & ~0xC0) << 8) + self._recv(1)[0]
        if c & 0xE0 == 0xC0:
            return int.from_bytes(bytes([c & ~0xE0]) + self._recv(2), "big")
        if c & 0xF0 == 0xE0:
            return int.from_bytes(bytes([c & ~0xF0]) + self._recv(3), "big")
        if c == 0xF0:
            return int.from_bytes(self._recv(4), "big")
        raise RouterError("bad length byte")

    def _send_sentence(self, words):
        out = b""
        for w in words:
            b = w.encode("utf-8")
            out += self._enc_len(len(b)) + b
        self.sock.sendall(out + b"\x00")

    def _read_sentence(self):
        words = []
        while True:
            n = self._read_len()
            if n == 0:
                return words
            words.append(self._recv(n).decode("utf-8", "replace"))

    def connect(self):
        try:
            raw = socket.create_connection((self.host, self.port), timeout=self.timeout)
            if self.use_ssl:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE  # routers typically use self-signed certs
                raw = ctx.wrap_socket(raw, server_hostname=self.host)
            self.sock = raw
            self.sock.settimeout(self.timeout)
        except OSError as e:
            raise RouterError(f"cannot connect to {self.host}:{self.port}: {e}")
        self.talk(["/login", f"=name={self.username}", f"=password={self.password}"])

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *a):
        self.close()

    def talk(self, words):
        self._send_sentence(words)
        rows, err = [], None
        while True:
            s = self._read_sentence()
            if not s:
                continue
            kind, attrs = s[0], s[1:]
            data = {}
            for a in attrs:
                if a.startswith("=") and "=" in a[1:]:
                    k, v = a[1:].split("=", 1)
                    data[k] = v
            if kind == "!re":
                rows.append(data)
            elif kind == "!trap":
                err = data.get("message", "router error")
            elif kind == "!fatal":
                raise RouterError("fatal: " + " ".join(attrs))
            elif kind == "!done":
                if err:
                    raise RouterError(err)
                return rows

    def run(self, path, query=None, **attrs):
        words = [path] + [f"={k.replace('_', '-') if k != 'id' else '.id'}={v}" for k, v in attrs.items()]
        words += [f"?{k}={v}" for k, v in (query or {}).items()]
        return self.talk(words)

    def _find(self, path, **q):
        rows = self.run(f"{path}/print", query=q)
        return rows[0] if rows else None

    # -- high level -------------------------------------------------------
    def resources(self):
        r = (self.run("/system/resource/print") or [{}])[0]
        ident = (self.run("/system/identity/print") or [{}])[0]
        total, free = int(r.get("total-memory", 0)), int(r.get("free-memory", 0))
        return {
            "identity": ident.get("name", ""),
            "cpu_load": int(r.get("cpu-load", 0)),
            "total_memory": total,
            "free_memory": free,
            "uptime": r.get("uptime", ""),
            "version": r.get("version", ""),
            "board": r.get("board-name", ""),
        }

    def ensure_profile(self, kind, name, rate_limit=""):
        path = "/ppp/profile" if kind == "pppoe" else "/ip/hotspot/user/profile"
        existing = self._find(path, name=name)
        attrs = {"rate_limit": rate_limit} if rate_limit else {}
        if existing:
            if attrs:
                self.run(f"{path}/set", id=existing[".id"], **attrs)
        else:
            self.run(f"{path}/add", name=name, **attrs)

    def upsert_user(self, kind, username, password, profile, disabled, mac="", ip="", limit_bytes=0):
        disabled = "yes" if disabled else "no"
        if kind == "pppoe":
            attrs = dict(name=username, password=password, service="pppoe", profile=profile, disabled=disabled)
            if mac:
                attrs["caller_id"] = mac
            if ip:
                attrs["remote_address"] = ip
            row = self._find("/ppp/secret", name=username)
            if row:
                attrs.setdefault("caller_id", "")
                self.run("/ppp/secret/set", id=row[".id"], **attrs)
            else:
                self.run("/ppp/secret/add", **attrs)
        else:
            attrs = dict(name=username, password=password, profile=profile, disabled=disabled)
            if limit_bytes:
                attrs["limit_bytes_total"] = limit_bytes
            if self.hotspot_server:
                attrs["server"] = self.hotspot_server
            row = self._find("/ip/hotspot/user", name=username)
            if row:
                self.run("/ip/hotspot/user/set", id=row[".id"], **attrs)
            else:
                self.run("/ip/hotspot/user/add", **attrs)

    def remove_user(self, kind, username):
        path = "/ppp/secret" if kind == "pppoe" else "/ip/hotspot/user"
        row = self._find(path, name=username)
        if row:
            self.run(f"{path}/remove", id=row[".id"])

    def active(self, kind):
        if kind == "pppoe":
            return [
                {"user": r.get("name", ""), "ip": r.get("address", ""), "mac": r.get("caller-id", ""),
                 "uptime": r.get("uptime", ""), "bytes_in": 0, "bytes_out": 0}
                for r in self.run("/ppp/active/print")
            ]
        return [
            {"user": r.get("user", ""), "ip": r.get("address", ""), "mac": r.get("mac-address", ""),
             "uptime": r.get("uptime", ""), "bytes_in": int(r.get("bytes-in", 0)),
             "bytes_out": int(r.get("bytes-out", 0))}
            for r in self.run("/ip/hotspot/active/print")
        ]

    def kick(self, kind, username):
        path = "/ppp/active" if kind == "pppoe" else "/ip/hotspot/active"
        key = "name" if kind == "pppoe" else "user"
        for row in self.run(f"{path}/print", query={key: username}):
            self.run(f"{path}/remove", id=row[".id"])

    def set_ip_binding(self, mac, ip, comment):
        """Hotspot IP binding: only this MAC (and IP) may be 'regular' on the hotspot."""
        row = self._find("/ip/hotspot/ip-binding", mac_address=mac)
        attrs = dict(mac_address=mac, type="regular", comment=comment)
        if ip:
            attrs["address"] = ip
        if self.hotspot_server:
            attrs["server"] = self.hotspot_server
        if row:
            self.run("/ip/hotspot/ip-binding/set", id=row[".id"], **attrs)
        else:
            self.run("/ip/hotspot/ip-binding/add", **attrs)

    def remove_ip_binding(self, comment):
        for row in self.run("/ip/hotspot/ip-binding/print", query={"comment": comment}):
            self.run("/ip/hotspot/ip-binding/remove", id=row[".id"])

    def interfaces_traffic(self):
        out = []
        for i in self.run("/interface/print"):
            name = i.get("name", "")
            try:
                m = self.talk(["/interface/monitor-traffic", f"=interface={name}", "=once="])
                m = m[0] if m else {}
            except RouterError:
                m = {}
            out.append({
                "name": name, "type": i.get("type", ""), "running": i.get("running") == "true",
                "rx_bps": int(m.get("rx-bits-per-second", 0)), "tx_bps": int(m.get("tx-bits-per-second", 0)),
                "rx_bytes": int(i.get("rx-byte", 0)), "tx_bytes": int(i.get("tx-byte", 0)),
            })
        return out

    def logs(self, topic="hotspot", limit=50):
        rows = self.run("/log/print")
        rows = [r for r in rows if topic in r.get("topics", "")] if topic else rows
        return [{"time": r.get("time", ""), "topics": r.get("topics", ""), "message": r.get("message", "")}
                for r in rows[-limit:]][::-1]


# --------------------------------------------------------------------------- simulation
_MOCK_LOCK = threading.Lock()
_MOCK_STATE = {}


def reset_mock():
    with _MOCK_LOCK:
        _MOCK_STATE.clear()


class MockRouter:
    """In-memory router that mimics RouterOSClient. State is shared per router key."""

    def __init__(self, key, hotspot_server=""):
        with _MOCK_LOCK:
            self.s = _MOCK_STATE.setdefault(key, {
                "profiles": {}, "users": {}, "active": {}, "bindings": {}, "log": [], "t0": time.time(),
            })
        self.key = key

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def connect(self):
        pass

    def close(self):
        pass

    def _log(self, topic, msg):
        self.s["log"].append({"time": time.strftime("%H:%M:%S"), "topics": topic, "message": msg})
        del self.s["log"][:-200]

    def resources(self):
        up = int(time.time() - self.s["t0"])
        return {"identity": f"mock-{self.key}", "cpu_load": random.randint(3, 35), "total_memory": 256 * 2**20,
                "free_memory": random.randint(100, 200) * 2**20, "uptime": f"{up // 3600}h{up % 3600 // 60}m",
                "version": "7.15 (mock)", "board": "RB-MOCK"}

    def ensure_profile(self, kind, name, rate_limit=""):
        self.s["profiles"][(kind, name)] = rate_limit

    def upsert_user(self, kind, username, password, profile, disabled, mac="", ip="", limit_bytes=0):
        self.s["users"][(kind, username)] = dict(password=password, profile=profile, disabled=bool(disabled),
                                                 mac=mac, ip=ip, limit_bytes=limit_bytes)
        self._log(kind, f"user {username} {'disabled' if disabled else 'enabled'}")

    def remove_user(self, kind, username):
        self.s["users"].pop((kind, username), None)
        self.s["active"].pop((kind, username), None)
        self._log(kind, f"user {username} removed")

    def active(self, kind):
        return [dict(v, user=k[1]) for k, v in self.s["active"].items() if k[0] == kind]

    def kick(self, kind, username):
        if self.s["active"].pop((kind, username), None):
            self._log(kind, f"{username} disconnected")

    def set_ip_binding(self, mac, ip, comment):
        self.s["bindings"][comment] = dict(mac=mac, ip=ip)

    def remove_ip_binding(self, comment):
        self.s["bindings"].pop(comment, None)

    def interfaces_traffic(self):
        return [{"name": n, "type": t, "running": True, "rx_bps": random.randint(10**5, 9 * 10**7),
                 "tx_bps": random.randint(10**5, 9 * 10**7), "rx_bytes": random.randint(10**9, 10**11),
                 "tx_bytes": random.randint(10**9, 10**11)}
                for n, t in (("ether1-wan", "ether"), ("ether2-lan", "ether"), ("bridge-hotspot", "bridge"))]

    def logs(self, topic="hotspot", limit=50):
        rows = [r for r in self.s["log"] if not topic or topic in r["topics"] or topic == "hotspot"]
        return rows[-limit:][::-1]

    # test / demo helper: simulate a user dialing in
    def simulate_login(self, kind, username, mac="AA:BB:CC:00:00:01", ip="10.0.0.2"):
        u = self.s["users"].get((kind, username))
        if not u or u["disabled"]:
            return False
        self.s["active"][(kind, username)] = dict(ip=ip, mac=mac, uptime="0s",
                                                  bytes_in=random.randint(10**6, 10**8),
                                                  bytes_out=random.randint(10**6, 10**8))
        self._log(kind, f"{username} logged in from {mac}")
        return True


def get_client(app, router):
    """Return a connected-on-enter client for a Router row."""
    if app.config.get("MIKROTIK_MOCK") or router.mock:
        return MockRouter(f"r{router.id}", router.hotspot_server)
    return RouterOSClient(router.host, router.port, router.username, router.password, router.use_ssl,
                          app.config["MIKROTIK_TIMEOUT"], router.hotspot_server)
