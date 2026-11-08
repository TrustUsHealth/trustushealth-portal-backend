"""Asynchronous email interface with local SMTP and Gmail SMTP transports."""

import asyncio
import logging
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage as MimeMessage
from email.utils import formataddr

from app.core.config import Settings

log = logging.getLogger(__name__)
RETRY_DELAYS_SECONDS = (0.5, 1.0)


@dataclass(frozen=True)
class EmailAttachment:
    filename: str
    content: bytes
    content_type: str = "application/octet-stream"


@dataclass(frozen=True)
class OutboundEmail:
    to: str
    subject: str
    text: str | None = None
    html: str | None = None
    attachments: tuple[EmailAttachment, ...] = field(default_factory=tuple)


class EmailDeliveryError(Exception):
    """Sanitized delivery failure; never includes message content or credentials."""


class Mailer:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def send_email_in_background(self, email: OutboundEmail) -> None:
        try:
            await self.send_email(email)
        except EmailDeliveryError:
            log.error("Background email was not delivered; no automatic queue is configured")

    async def send_email(self, email: OutboundEmail) -> None:
        message = self._build_message(email)
        for attempt in range(len(RETRY_DELAYS_SECONDS) + 1):
            try:
                await asyncio.to_thread(self._send_sync, message)
                return
            except smtplib.SMTPResponseException as exc:
                retryable = 400 <= exc.smtp_code < 500
                category = "temporary_smtp_response" if retryable else "permanent_smtp_response"
            except (OSError, TimeoutError, smtplib.SMTPServerDisconnected):
                retryable = True
                category = "connection"
            except smtplib.SMTPException:
                retryable = False
                category = "smtp"

            if retryable and attempt < len(RETRY_DELAYS_SECONDS):
                log.warning(
                    "Email delivery attempt %d failed (transport=%s, category=%s); retrying",
                    attempt + 1,
                    self.settings.mail_transport,
                    category,
                )
                await asyncio.sleep(RETRY_DELAYS_SECONDS[attempt])
                continue

            log.error(
                "Email delivery failed after %d attempt(s) (transport=%s, category=%s)",
                attempt + 1,
                self.settings.mail_transport,
                category,
            )
            raise EmailDeliveryError("Email delivery failed.") from None

    def _build_message(self, email: OutboundEmail) -> MimeMessage:
        if email.text is None and email.html is None:
            raise ValueError("An email requires a text or HTML body.")

        message = MimeMessage()
        if self.settings.mail_transport == "gmail":
            if self.settings.gmail_address is None:
                raise RuntimeError("Gmail sender is not configured.")
            sender = str(self.settings.gmail_address)
            message["From"] = formataddr(("TrustUs Health", sender))
        else:
            message["From"] = "TrustUs Health <no-reply@localhost>"
        message["To"] = email.to
        message["Subject"] = email.subject

        if email.text is not None:
            message.set_content(email.text)
            if email.html is not None:
                message.add_alternative(email.html, subtype="html")
        else:
            message.set_content(email.html or "", subtype="html")

        for attachment in email.attachments:
            if "/" not in attachment.content_type:
                raise ValueError(f"Invalid attachment content type: {attachment.content_type!r}")
            maintype, subtype = attachment.content_type.split("/", maxsplit=1)
            filename = attachment.filename.replace("\\", "/").rsplit("/", maxsplit=1)[-1]
            if not filename:
                raise ValueError("An email attachment requires a filename.")
            message.add_attachment(
                attachment.content,
                maintype=maintype,
                subtype=subtype,
                filename=filename,
            )
        return message

    def _send_sync(self, message: MimeMessage) -> None:
        if self.settings.mail_transport == "gmail":
            self._send_gmail(message)
            return

        with smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port, timeout=15) as smtp:
            smtp.send_message(message)

    def _send_gmail(self, message: MimeMessage) -> None:
        address = self.settings.gmail_address
        password = self.settings.gmail_app_password
        if address is None or password is None:
            raise RuntimeError("Gmail sender is not configured.")

        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15, context=ssl.create_default_context()) as smtp:
            smtp.login(str(address), password.get_secret_value().replace(" ", ""))
            smtp.send_message(message)
