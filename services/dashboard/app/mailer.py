"""Outbound email with two backends.

`manual` is the default because this project has no SMTP relay yet: the
message is not sent at all. `send()` records which backend actually
handled the message (`user_tokens.delivered_via` is set to `'manual'` or
`'smtp'`), but the raw verification/reset link itself is never logged or
otherwise exposed — raw tokens must never appear in logs, responses, or
command-line arguments. In manual mode an administrator currently has no
in-app way to retrieve or forward that link; see "Accounts and roles" in
docs/operations.md for the workarounds (approve reviewers directly rather
than waiting on email verification, etc.). Switching to `smtp` later is a
configuration change, not a rewrite.

A failing relay degrades to `manual` rather than raising — a dead mail
server should not turn a password reset into a 500 for the user."""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

log = logging.getLogger(__name__)


def reset_email(link: str) -> tuple[str, str]:
    return (
        "Wahoo Bay dashboard: password reset",
        "A password reset was requested for your Wahoo Bay account.\n\n"
        f"{link}\n\n"
        "The link expires in 30 minutes and can be used once. If you did not "
        "request this, you can ignore this message — nothing has changed.\n",
    )


def verify_email(link: str) -> tuple[str, str]:
    return (
        "Wahoo Bay dashboard: confirm your email",
        "Confirm your email address for your Wahoo Bay account:\n\n"
        f"{link}\n\n"
        "An administrator still needs to approve the account before you can "
        "review detections.\n",
    )


def collision_notice() -> tuple[str, str]:
    return (
        "Wahoo Bay dashboard: registration attempt",
        "Someone tried to register a Wahoo Bay account with this email "
        "address, which already has one. No new account was created and "
        "nothing has changed.\n\nIf that was you, sign in normally or use "
        "the password reset link on the sign-in page.\n",
    )


class Mailer:
    def __init__(
        self, backend: str, smtp: dict | None, from_addr: str, base_url: str
    ) -> None:
        if backend not in ("smtp", "manual"):
            log.warning(
                "mailer: unrecognized backend '%s'; falling back to manual", backend
            )
            self.backend = "manual"
        else:
            self.backend = backend
        self.smtp = smtp or {}
        self.from_addr = from_addr
        self.base_url = base_url.rstrip("/")

        # Validate SMTP config if backend is smtp
        if self.backend == "smtp":
            missing = []
            if not self.smtp.get("host"):
                missing.append("host")
            if self.smtp.get("port") is None or self.smtp.get("port") == "":
                missing.append("port")
            if missing:
                log.error(
                    "mailer(smtp): required config missing (%s); falling back to manual",
                    ", ".join(missing),
                )
                self.backend = "manual"

    def link(self, path: str, token: str) -> str:
        return f"{self.base_url}{path}?token={token}"

    def send(self, to: str, subject: str, body: str) -> str:
        """Returns the backend that actually handled the message."""
        if self.backend != "smtp":
            log.info(
                "mailer(manual): message for %s held for out-of-band delivery", to
            )
            return "manual"
        msg = EmailMessage()
        msg["From"] = self.from_addr
        msg["To"] = to
        msg["Subject"] = subject
        # Gmail refuses a message without a valid Message-ID, after the
        # relay has already accepted it, so the loss never shows here
        # (2026-09-26). RFC 5322 requires Date as well.
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=self.from_addr.rpartition("@")[2] or None)
        msg.set_content(body)
        try:
            with smtplib.SMTP(self.smtp["host"], self.smtp["port"], timeout=10) as s:
                if self.smtp.get("starttls"):
                    # Explicit context, not the bare starttls() default: on
                    # 3.11 that falls back to check_hostname=False,
                    # verify_mode=CERT_NONE — encrypted but unauthenticated,
                    # so an on-path attacker could MITM the relay
                    # connection and capture credentials and every reset
                    # link. create_default_context() verifies the
                    # relay's certificate and hostname.
                    s.starttls(context=ssl.create_default_context())
                if self.smtp.get("user"):
                    s.login(self.smtp["user"], self.smtp["password"])
                s.send_message(msg)
            return "smtp"
        except Exception as e:
            log.warning(
                "mailer(smtp): delivery to %s failed (%s); holding for manual", to, e
            )
            return "manual"
