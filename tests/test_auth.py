"""Unit tests for services/dashboard/app/auth.py.

Pure functions only — no database, no FastAPI client. The DB-backed
session and token stores are covered in tests/test_auth_db.py."""

from __future__ import annotations

import re
import ssl
import sys
from email.utils import parsedate_to_datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "dashboard"))

import pytest  # noqa: E402
from app.auth import (  # noqa: E402
    MAX_PASSWORD_LEN,
    check_password_policy,
    hash_password,
    verify_password,
)


def test_hash_then_verify_roundtrip():
    stored = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", stored) is True


def test_verify_rejects_wrong_password():
    stored = hash_password("correct horse battery staple")
    assert verify_password("Correct horse battery staple", stored) is False


def test_hash_is_salted():
    a = hash_password("same password twice")
    b = hash_password("same password twice")
    assert a != b
    assert verify_password("same password twice", a)
    assert verify_password("same password twice", b)


def test_verify_reads_cost_parameters_from_stored_value():
    # A hash written with a lower cost must still verify, so parameters
    # can be raised later without invalidating existing accounts.
    weak = hash_password("legacy parameters", n=1024)
    assert weak.startswith("scrypt$1024$")
    assert verify_password("legacy parameters", weak) is True


def test_verify_returns_false_on_malformed_stored_value():
    for junk in ("", "not-a-hash", "scrypt$abc$8$1$xx$yy", "scrypt$16384$8$1$only-four"):
        assert verify_password("anything", junk) is False


def test_policy_accepts_long_passphrase_without_symbols():
    assert check_password_policy("salt water fish counting", "dan", "dan@fau.edu") is None


def test_policy_rejects_short_password():
    msg = check_password_policy("Tr0ub4dor&3", "dan", "dan@fau.edu")
    assert msg is not None and "12" in msg


def test_policy_rejects_password_containing_username_or_email_local_part():
    assert check_password_policy("dan-is-here-okay", "dan", "x@fau.edu") is not None
    assert check_password_policy("scoopnisker-reef", "u", "scoopnisker@fau.edu") is not None


def test_policy_rejects_denylisted_password():
    assert check_password_policy("password123456", "u", "u@e.org") is not None


def test_policy_rejects_oversized_password():
    # Review Focus 1: an unbounded password is a memory/CPU footgun and
    # must fail validation, not raise.
    msg = check_password_policy("a" * (MAX_PASSWORD_LEN + 1), "u", "u@e.org")
    assert msg is not None and "long" in msg.lower()


def test_hash_password_rejects_oversized_password():
    with pytest.raises(ValueError):
        hash_password("a" * (MAX_PASSWORD_LEN + 1))


from app.mailer import Mailer, approved_email, reset_email, verify_email  # noqa: E402


def test_the_sign_up_email_says_the_request_is_waiting_for_approval():
    # Owner's request 2026-10-01: someone who registers is told at once that
    # their request arrived and is waiting for an admin, not only asked to
    # confirm an address.
    subject, body = verify_email("https://example.org/verify-email?token=abc", hours=24)
    assert "account request" in subject.lower()
    assert "waiting for an administrator to approve" in body
    assert "https://example.org/verify-email?token=abc" in body
    assert "24 hours" in body
    assert body.index("waiting for an administrator") < body.index("https://example.org")


@pytest.mark.parametrize(
    "role, says",
    [
        ("viewer", "live cameras"),
        ("reviewer", "review site"),
        ("admin", "manage accounts"),
    ],
)
def test_the_approval_email_says_what_the_account_can_do(role, says):
    subject, body = approved_email("https://example.org/login", role)
    assert "approved" in subject.lower()
    assert "https://example.org/login" in body and says in body


def test_manual_backend_does_not_attempt_smtp(monkeypatch):
    def explode(*a, **k):
        raise AssertionError("SMTP must not be contacted in manual mode")

    monkeypatch.setattr("smtplib.SMTP", explode)
    m = Mailer(backend="manual", smtp=None, from_addr="x@y.org", base_url="https://example.org")
    assert m.send("someone@example.org", "Subject", "Body") == "manual"


def test_smtp_backend_sends_one_message(monkeypatch):
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self, context=None):
            sent.append(("starttls", context))

        def login(self, user, password):
            sent.append(("login", user))

        def send_message(self, msg):
            sent.append(("send", msg["To"], msg["Subject"]))

    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    m = Mailer(
        backend="smtp",
        smtp={"host": "relay.fau.edu", "port": 587, "user": "u", "password": "p", "starttls": True},
        from_addr="noreply@example.org",
        base_url="https://example.org",
    )
    assert m.send("someone@example.org", "Hello", "Body") == "smtp"
    assert ("send", "someone@example.org", "Hello") in sent

    # D8: starttls() must be called with an explicit, verifying SSL
    # context — not the bare call, which on 3.11 falls back to
    # check_hostname=False/verify_mode=CERT_NONE (encrypted but
    # unauthenticated, letting an on-path attacker MITM the relay and
    # capture credentials and every reset link). Pin the actual
    # properties create_default_context() guarantees, not just "some
    # context was passed" — a context object with verification disabled
    # would still satisfy an `is not None` check alone.
    starttls_calls = [c for c in sent if c[0] == "starttls"]
    assert len(starttls_calls) == 1
    context = starttls_calls[0][1]
    assert context is not None
    assert isinstance(context, ssl.SSLContext)
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED


def test_smtp_messages_carry_a_date_and_a_message_id(monkeypatch):
    # 2026-09-26: Gmail silently dropped the dashboard's confirmation
    # emails. The relay (Resend) accepted them, but Gmail refuses
    # messages without a valid Message-ID, and the mailer set only
    # From/To/Subject. RFC 5322 requires Date and expects Message-ID.
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self, context=None):
            pass

        def login(self, user, password):
            pass

        def send_message(self, msg):
            sent.append(msg)

    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    m = Mailer(
        backend="smtp",
        smtp={"host": "h", "port": 587, "starttls": True},
        from_addr="noreply@wahoobay.org",
        base_url="https://example.org",
    )
    m.send("a@example.org", "One", "Body")
    m.send("a@example.org", "Two", "Body")
    first, second = sent
    for msg in sent:
        assert parsedate_to_datetime(msg["Date"]).tzinfo is not None
        assert re.fullmatch(r"<[^<>@\s]+@wahoobay\.org>", msg["Message-ID"])
    assert first["Message-ID"] != second["Message-ID"]


def test_smtp_failure_falls_back_to_manual_rather_than_raising(monkeypatch):
    def explode(*a, **k):
        raise OSError("relay refused")

    monkeypatch.setattr("smtplib.SMTP", explode)
    m = Mailer(
        backend="smtp",
        smtp={"host": "h", "port": 25, "user": None, "password": None, "starttls": False},
        from_addr="x@y.org",
        base_url="https://example.org",
    )
    # A dead relay must not turn a password reset into a 500.
    assert m.send("someone@example.org", "S", "B") == "manual"


def test_reset_email_contains_the_link_and_no_token_elsewhere():
    subject, body = reset_email("https://example.org/reset-password?token=abc123")
    assert "abc123" in body
    assert "abc123" not in subject
    assert subject and "\n" not in subject


def test_unrecognized_backend_logs_warning_and_falls_back_to_console(caplog):
    m = Mailer(
        backend="SMTP",
        smtp={"host": "h", "port": 25},
        from_addr="x@y.org",
        base_url="https://example.org",
    )
    assert m.backend == "console"  # the message goes to the log, never silently nowhere
    assert "unrecognized backend 'SMTP'" in caplog.text
    assert "falling back to console" in caplog.text


def test_smtp_missing_config_logs_error_and_falls_back_to_manual(monkeypatch, caplog):
    def explode(*a, **k):
        raise AssertionError("SMTP must not be contacted when config invalid")

    monkeypatch.setattr("smtplib.SMTP", explode)
    m = Mailer(
        backend="smtp", smtp={"port": 587}, from_addr="x@y.org", base_url="https://example.org"
    )
    assert m.backend == "manual"
    assert "required config missing" in caplog.text
    assert "host" in caplog.text
    # Verify send() also doesn't attempt SMTP
    assert m.send("someone@example.org", "S", "B") == "manual"
