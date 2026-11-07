"""Plain-text email. Local: SMTP to Mailpit. Production: Resend. No logos, ever."""

import asyncio
import logging
import smtplib
from email.message import EmailMessage

import httpx

from app.core.config import Settings

log = logging.getLogger(__name__)


class Mailer:
    def __init__(self, settings: Settings):
        self.s = settings

    async def send(self, to: str, subject: str, body: str) -> None:
        try:
            if self.s.mail_transport == "resend":
                await self._resend(to, subject, body)
            else:
                await asyncio.to_thread(self._smtp, to, subject, body)
        except Exception:  # noqa: BLE001  never log the body: it can hold an OTP
            log.exception("email send failed (transport=%s)", self.s.mail_transport)

    def _smtp(self, to: str, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.s.mail_from, to, subject
        msg.set_content(body)
        with smtplib.SMTP(self.s.smtp_host, self.s.smtp_port, timeout=10) as smtp:
            smtp.send_message(msg)

    async def _resend(self, to: str, subject: str, body: str) -> None:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {self.s.resend_api_key}"},
                json={"from": self.s.mail_from, "to": [to], "subject": subject, "text": body},
            )
            r.raise_for_status()
