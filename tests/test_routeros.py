"""Exercise the real RouterOS API client against a tiny fake API server."""
import socket
import threading

import pytest

from app.mikrotik import RouterError, RouterOSClient


def enc(n):
    return RouterOSClient._enc_len(n)


def sentence(words):
    return b"".join(enc(len(w.encode())) + w.encode() for w in words) + b"\x00"


class FakeRouter(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.received = []
        self.secrets = []

    def read_sentence(self, conn):
        c = RouterOSClient("x")
        c.sock = conn
        return c._read_sentence()

    def run(self):
        conn, _ = self.srv.accept()
        while True:
            try:
                s = self.read_sentence(conn)
            except Exception:
                return
            self.received.append(s)
            cmd = s[0]
            if cmd == "/login":
                ok = "=password=good" in s
                conn.sendall(sentence(["!done"]) if ok else sentence(["!trap", "=message=invalid user name or password"]) + sentence(["!done"]))
            elif cmd == "/ppp/secret/print":
                for r in self.secrets:
                    if not any(w.startswith("?name=") and w[6:] != r["name"] for w in s):
                        conn.sendall(sentence(["!re", f"=.id={r['id']}", f"=name={r['name']}"]))
                conn.sendall(sentence(["!done"]))
            elif cmd == "/ppp/secret/add":
                self.secrets.append({"id": "*1", "name": s[1].split("=", 2)[2]})
                conn.sendall(sentence(["!done", "=ret=*1"]))
            elif cmd == "/system/resource/print":
                conn.sendall(sentence(["!re", "=cpu-load=7", "=total-memory=1000", "=free-memory=250",
                                       "=uptime=1d", "=version=7.14", "=board-name=hAP"]) + sentence(["!done"]))
            elif cmd == "/system/identity/print":
                conn.sendall(sentence(["!re", "=name=core"]) + sentence(["!done"]))
            else:
                conn.sendall(sentence(["!trap", "=message=no such command"]) + sentence(["!done"]))


@pytest.fixture
def fake():
    f = FakeRouter()
    f.start()
    return f


def test_length_encoding_roundtrip():
    for n in (0, 1, 127, 128, 16383, 16384, 2097151, 2097152):
        e = enc(n)
        c = RouterOSClient("x")

        class S:
            def __init__(s, b): s.b = b
            def recv(s, k):
                r, s.b = s.b[:k], s.b[k:]
                return r
        c.sock = S(e)
        assert c._read_len() == n


def test_login_resources_and_upsert(fake):
    with RouterOSClient("127.0.0.1", fake.port, "admin", "good", timeout=3) as c:
        r = c.resources()
        assert r["cpu_load"] == 7 and r["identity"] == "core" and r["free_memory"] == 250
        c.upsert_user("pppoe", "bob", "pw", "prof", disabled=True, mac="AA:BB:CC:DD:EE:FF")
        add = [s for s in fake.received if s[0] == "/ppp/secret/add"][0]
        assert "=name=bob" in add and "=disabled=yes" in add and "=caller-id=AA:BB:CC:DD:EE:FF" in add
        assert "=profile=prof" in add and "=service=pppoe" in add


def test_bad_credentials_and_trap(fake):
    with pytest.raises(RouterError, match="invalid user"):
        RouterOSClient("127.0.0.1", fake.port, "admin", "bad", timeout=3).connect()


def test_unreachable():
    with pytest.raises(RouterError):
        RouterOSClient("127.0.0.1", 1, timeout=1).connect()
