import csv
import io
import re
import secrets
import string
from datetime import datetime, timedelta, timezone

from flask import Response, current_app


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def money(v):
    return round(float(v or 0) + 1e-9, 2)


def local(dt):
    if not dt:
        return None
    return dt + timedelta(hours=current_app.config["TZ_OFFSET_HOURS"])


def fmt_dt(dt):
    d = local(dt)
    return d.strftime("%d-%b-%Y %H:%M") if d else "-"


def fmt_date(dt):
    d = local(dt)
    return d.strftime("%d-%b-%Y") if d else "-"


def local_day_range(day=None):
    """UTC [start, end) for a local calendar day (default today)."""
    off = timedelta(hours=current_app.config["TZ_OFFSET_HOURS"])
    d = day or (utcnow() + off).date()
    start = datetime(d.year, d.month, d.day) - off
    return start, start + timedelta(days=1)


def local_month_range(year=None, month=None):
    off = timedelta(hours=current_app.config["TZ_OFFSET_HOURS"])
    now = utcnow() + off
    y, m = year or now.year, month or now.month
    start = datetime(y, m, 1) - off
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    return start, datetime(ny, nm, 1) - off


def gen_password(n=8):
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def norm_phone(p):
    """Normalise Bangladeshi mobile numbers to 01XXXXXXXXX."""
    p = re.sub(r"[^\d+]", "", p or "")
    if p.startswith("+880"):
        p = "0" + p[4:]
    elif p.startswith("880"):
        p = "0" + p[3:]
    return p


def norm_mac(m):
    m = re.sub(r"[^0-9a-fA-F]", "", m or "").upper()
    if len(m) != 12:
        return ""
    return ":".join(m[i:i + 2] for i in range(0, 12, 2))


def human_bytes(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} PB"


def csv_response(filename, header, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return Response(
        "﻿" + buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
