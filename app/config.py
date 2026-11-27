import os


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL", "sqlite:///isp.db")
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # session / cookie security
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") == "1"
    PERMANENT_SESSION_LIFETIME_MIN = int(os.environ.get("SESSION_MINUTES", "120"))

    # use the in-memory simulated router instead of a real MikroTik (dev/demo)
    MIKROTIK_MOCK = os.environ.get("MIKROTIK_MOCK", "0") == "1"
    MIKROTIK_TIMEOUT = float(os.environ.get("MIKROTIK_TIMEOUT", "5"))

    # background automation loop (seconds, 0 = disabled; use `flask automation` + cron instead)
    AUTOMATION_INTERVAL = int(os.environ.get("AUTOMATION_INTERVAL", "0"))

    TRUST_PROXY = os.environ.get("TRUST_PROXY", "0") == "1"

    # login throttling
    MAX_LOGIN_FAILS = 5
    LOGIN_LOCK_MINUTES = 15

    # Asia/Dhaka
    TZ_OFFSET_HOURS = 6

    # public base URL used for payment-gateway callbacks
    BASE_URL = os.environ.get("BASE_URL", "http://localhost:5000")


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite://"
    MIKROTIK_MOCK = True
    SECRET_KEY = "test"
    WTF_CSRF = False
