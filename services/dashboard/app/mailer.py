"""Outbound email with three backends (smtp, ses, console) and a holding state (manual).

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


def verify_email(link: str, hours: int = 24) -> tuple[str, str]:
    """Sent the moment someone registers: their request arrived and is
    waiting for an administrator (owner's request 2026-10-01), plus the link
    that confirms their address."""
    return (
        "Wahoo Bay Live Fish ID: we've received your account request",
        "Thanks for requesting an account for Wahoo Bay Live Fish ID.\n\n"
        "Your request is waiting for an administrator to approve it. Until "
        'then, signing in shows a "waiting for approval" page.\n\n'
        "Please confirm your email address in the meantime:\n\n"
        f"{link}\n\n"
        f"The link expires in {hours} hours. If you didn't request an account, "
        "you can ignore this message.\n",
    )


# What each role opens up, for the approval email.
ROLE_ACCESS = {
    "viewer": "You can now see the live cameras, the fish they record and the data.",
    "reviewer": (
        "You can now see the live cameras, the fish they record and the data, "
        "and help identify fish on the review site."
    ),
    "admin": (
        "You can now see everything on the site, including the review site, and " "manage accounts."
    ),
}


def approved_email(login_link: str, role: str) -> tuple[str, str]:
    """Sent when an administrator approves an account (owner's request
    2026-10-01): with the site behind sign-in, this is how a new member
    learns they're in."""
    return (
        "Wahoo Bay Live Fish ID: your account is approved",
        "Your Wahoo Bay Live Fish ID account has been approved.\n\n"
        f"{ROLE_ACCESS.get(role, ROLE_ACCESS['viewer'])}\n\n"
        f"Sign in here:\n\n{login_link}\n",
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
    def __init__(self, backend: str, smtp: dict | None, from_addr: str, base_url: str) -> None:
        if backend not in ("smtp", "ses", "manual", "console"):
            log.warning("mailer: unrecognized backend '%s'; falling back to console", backend)
            self.backend = "console"
        else:
            self.backend = backend
        self.smtp = smtp or {}
        self.from_addr = from_addr
        self.base_url = base_url.rstrip("/")

        # SES sends through the process's own AWS identity (the App Runner role on AWS): no host,
        # no credentials, but the sender must be configured; never guess one.
        if self.backend == "ses" and not self.from_addr:
            log.error("mailer(ses): MAIL_FROM is required; falling back to manual")
            self.backend = "manual"
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

    def link_to(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def send(self, to: str, subject: str, body: str) -> str:
        """Returns the backend that actually handled the message."""
        if self.backend == "console":
            # The whole message, link included, to the log and nowhere else: a
            # developer or an operator reads it there (staging, local).
            log.info("mailer(console): to=%s subject=%r\n%s", to, subject, body)
            return "console"
        if self.backend == "ses":
            return self._send_ses(to, subject, body)
        if self.backend != "smtp":
            log.info("mailer(manual): message for %s held for out-of-band delivery", to)
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
            log.warning("mailer(smtp): delivery to %s failed (%s); holding for manual", to, e)
            return "manual"

    def _send_ses(self, to: str, subject: str, body: str) -> str:
        """Amazon SES v2 with the credentials the environment provides (on AWS, the service's role).
        A refusal (sandbox, unverified sender, throttling) holds the message like an SMTP failure."""
        import boto3  # the dashboard image has it for S3; imported here so the other backends need nothing

        try:
            boto3.client("sesv2").send_email(
                FromEmailAddress=self.from_addr,
                Destination={"ToAddresses": [to]},
                Content={
                    "Simple": {
                        "Subject": {"Data": subject, "Charset": "UTF-8"},
                        "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
                    }
                },
            )
            return "ses"
        except Exception as e:  # noqa: BLE001 - any refusal means "held", and the reason goes to the log
            log.warning("mailer(ses): delivery to %s failed (%s); holding for manual", to, e)
            return "manual"
