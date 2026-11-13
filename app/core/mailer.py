"""Asynchronous email interface with local SMTP and Gmail SMTP transports."""

import asyncio
import base64
import logging
import secrets
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage as MimeMessage
from email.utils import formataddr
from pathlib import Path

import httpx

from app.core.config import Settings
from app.core.email_templates import render_branded_email, text_to_html

log = logging.getLogger(__name__)
RETRY_DELAYS_SECONDS = (0.5, 1.0)
LOGO_CONTENT_ID = "trustus-health-logo"
LOGO_PATH = Path(__file__).with_name("assets") / "brand-logo.png"
GOOGLE_SCRIPT_TIMEOUT_SECONDS = 20


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
    # Optional trusted HTML body fragment; plain text is escaped and rendered when omitted.
    html: str | None = None
    attachments: tuple[EmailAttachment, ...] = field(default_factory=tuple)


class EmailDeliveryError(Exception):
    """Sanitized delivery failure; never includes message content or credentials."""


class _DeliveryAttemptError(Exception):
    def __init__(self, category: str, retryable: bool):
        self.category = category
        self.retryable = retryable


class Mailer:
    def __init__(self, settings: Settings, brand_name: str = "TrustUs Health"):
        self.settings = settings
        self.brand_name = brand_name

    async def send_email_in_background(self, email: OutboundEmail) -> None:
        try:
            await self.send_email(email)
        except EmailDeliveryError:
            log.error("Background email was not delivered; no automatic queue is configured")

    async def send_email(self, email: OutboundEmail) -> None:
        message = self._build_message(email)
        idempotency_key = secrets.token_urlsafe(32)
        for attempt in range(len(RETRY_DELAYS_SECONDS) + 1):
            try:
                if self.settings.mail_transport == "google_apps_script":
                    await self._send_google_apps_script(message, idempotency_key)
                else:
                    await asyncio.to_thread(self._send_sync, message)
                return
            except _DeliveryAttemptError as exc:
                retryable, category = exc.retryable, exc.category
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

    async def _send_google_apps_script(self, message: MimeMessage, idempotency_key: str) -> None:
        url = self.settings.google_script_url
        secret = self.settings.google_script_secret
        if not url or not secret:
            raise RuntimeError("Google Apps Script email transport is not configured.")

        payload = self._google_script_payload(message, idempotency_key, secret.get_secret_value())
        try:
            async with httpx.AsyncClient(
                timeout=GOOGLE_SCRIPT_TIMEOUT_SECONDS,
                follow_redirects=True,
            ) as client:
                response = await client.post(url, json=payload)
        except httpx.TransportError:
            raise _DeliveryAttemptError("connection", True) from None
        except httpx.HTTPError:
            raise _DeliveryAttemptError("http", False) from None

        if response.status_code == 429 or response.status_code >= 500:
            raise _DeliveryAttemptError("http_response", True)
        if not 200 <= response.status_code < 300:
            raise _DeliveryAttemptError("http_response", False)
        try:
            result = response.json()
        except ValueError:
            raise _DeliveryAttemptError("invalid_response", False) from None
        if not isinstance(result, dict):
            raise _DeliveryAttemptError("invalid_response", False)
        if result.get("idempotencyKey") != idempotency_key:
            raise _DeliveryAttemptError("invalid_response", False)
        if result.get("error") == "busy":
            raise _DeliveryAttemptError("script_busy", True)
        if result.get("ok") is not True:
            raise _DeliveryAttemptError("script_rejected", False)

    @staticmethod
    def _google_script_payload(message: MimeMessage, idempotency_key: str, secret: str) -> dict:
        text, html = "", ""
        inline_images = {}
        attachments = []
        for part in message.walk():
            if part.is_multipart():
                continue
            content_type = part.get_content_type()
            if part.get_content_disposition() == "attachment":
                attachments.append(
                    {
                        "filename": part.get_filename(),
                        "contentType": content_type,
                        "content": base64.b64encode(part.get_payload(decode=True) or b"").decode("ascii"),
                    }
                )
            elif content_type == "text/plain":
                text = part.get_content()
            elif content_type == "text/html":
                html = part.get_content()
            elif part.get("Content-ID"):
                content_id = str(part["Content-ID"]).strip("<>")
                inline_images[content_id] = {
                    "contentType": content_type,
                    "content": base64.b64encode(part.get_payload(decode=True) or b"").decode("ascii"),
                }

        return {
            "secret": secret,
            "idempotencyKey": idempotency_key,
            "to": str(message["To"]),
            "subject": str(message["Subject"]),
            "text": text,
            "html": html,
            "inlineImages": inline_images,
            "attachments": attachments,
        }

    def _build_message(self, email: OutboundEmail) -> MimeMessage:
        if email.text is None and email.html is None:
            raise ValueError("An email requires a text or HTML body.")

        message = MimeMessage()
        if self.settings.mail_transport == "gmail":
            if self.settings.gmail_address is None:
                raise RuntimeError("Gmail sender is not configured.")
            sender = str(self.settings.gmail_address)
            message["From"] = formataddr((self.brand_name, sender))
        else:
            message["From"] = formataddr((self.brand_name, "no-reply@localhost"))
        message["To"] = email.to
        message["Subject"] = email.subject

        text = email.text or ""
        content_html = email.html if email.html is not None else text_to_html(text)
        html = render_branded_email(self.brand_name, email.subject, content_html)
        message.set_content(text)
        message.add_alternative(html, subtype="html")
        html_part = message.get_body(preferencelist=("html",))
        if html_part is None:
            raise RuntimeError("The branded HTML email body could not be created.")
        html_part.add_related(
            LOGO_PATH.read_bytes(),
            maintype="image",
            subtype="png",
            cid=f"<{LOGO_CONTENT_ID}>",
        )

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
