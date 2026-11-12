import logging
import smtplib
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings
from app.core.mailer import EmailAttachment, EmailDeliveryError, Mailer, OutboundEmail


def make_settings(**overrides):
    values = {
        "_env_file": None,
        "environment": "test",
        "mongodb_uri": "mongodb://unused",
        "jwt_secret": "t" * 40,
    }
    values.update(overrides)
    return Settings(**values)


def test_gmail_transport_requires_an_address_and_app_password():
    with pytest.raises(ValidationError, match="GMAIL_ADDRESS is required"):
        make_settings(mail_transport="gmail", gmail_app_password=SecretStr("app-password"))

    with pytest.raises(ValidationError, match="GMAIL_APP_PASSWORD is required"):
        make_settings(mail_transport="gmail", gmail_address="sender@gmail.com")


def test_message_supports_text_html_and_pdf_attachments():
    mailer = Mailer(
        make_settings(mail_transport="gmail", gmail_address="sender@gmail.com", gmail_app_password="secret")
    )
    message = mailer._build_message(
        OutboundEmail(
            to="recipient@example.com",
            subject="Report",
            text="Plain text",
            html="<p>HTML</p>",
            attachments=(
                EmailAttachment(
                    filename="reports\\result.pdf",
                    content=b"%PDF-1.7",
                    content_type="application/pdf",
                ),
            ),
        )
    )

    assert message["From"] == "TrustUs Health <sender@gmail.com>"
    assert message["To"] == "recipient@example.com"
    assert message.get_content_type() == "multipart/mixed"
    assert message.get_body(preferencelist=("plain",)).get_content() == "Plain text\n"
    html_part = message.get_body(preferencelist=("html",))
    html_content = html_part.get_body(preferencelist=("html",)).get_content()
    assert "TrustUs Health" in html_content
    assert "cid:trustus-health-logo" in html_content
    logo = next(part for part in message.walk() if part.get_content_type() == "image/png")
    assert logo.get_content_type() == "image/png"
    assert logo["Content-ID"] == "<trustus-health-logo>"
    attachment = next(message.iter_attachments())
    assert attachment.get_content_type() == "application/pdf"
    assert attachment.get_filename() == "result.pdf"
    assert attachment.get_payload(decode=True) == b"%PDF-1.7"


def test_text_email_gets_branded_html_and_escapes_text():
    mailer = Mailer(make_settings(), brand_name="TrustUs & Health")
    message = mailer._build_message(
        OutboundEmail(to="recipient@example.com", subject="A <notice>", text="First line\nSecond <line>")
    )

    html_part = message.get_body(preferencelist=("html",))
    html_content = html_part.get_body(preferencelist=("html",)).get_content()
    assert "TrustUs &amp; Health" in html_content
    assert "A &lt;notice&gt;" in html_content
    assert "Second &lt;line&gt;" in html_content
    assert "cid:trustus-health-logo" in html_content
    assert "First line<br>" in html_content


def test_email_with_html_keeps_plain_text_alternative_and_embeds_logo():
    mailer = Mailer(make_settings())
    message = mailer._build_message(
        OutboundEmail(
            to="recipient@example.com",
            subject="Login code",
            text="Your code is 123456",
            html="<b>123456</b>",
        )
    )

    assert message.get_body(preferencelist=("plain",)).get_content() == "Your code is 123456\n"
    html_part = message.get_body(preferencelist=("html",))
    html_content = html_part.get_body(preferencelist=("html",)).get_content()
    assert "<b>123456</b>" in html_content
    assert "cid:trustus-health-logo" in html_content


def test_gmail_transport_uses_tls_and_the_app_password(monkeypatch):
    mailer = Mailer(
        make_settings(
            mail_transport="gmail",
            gmail_address="sender@gmail.com",
            gmail_app_password="abcd efgh ijkl mnop",
        )
    )
    calls = {}

    class SmtpStub:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def login(self, address, password):
            calls["credentials"] = (address, password)

        def send_message(self, message):
            calls["message"] = message

    def smtp_ssl(host, port, **kwargs):
        calls["connection"] = (host, port, kwargs)
        return SmtpStub()

    monkeypatch.setattr("app.core.mailer.smtplib.SMTP_SSL", smtp_ssl)
    mailer._send_sync(mailer._build_message(OutboundEmail(to="recipient@example.com", subject="Hi", text="Hello")))

    host, port, options = calls["connection"]
    assert host == "smtp.gmail.com" and port == 465
    assert options["context"].check_hostname
    assert calls["credentials"] == ("sender@gmail.com", "abcdefghijklmnop")
    assert calls["message"]["To"] == "recipient@example.com"


async def test_temporary_smtp_failure_is_retried_without_logging_email_content(monkeypatch, caplog):
    mailer = Mailer(make_settings())
    attempts = 0

    def fail_once(_message):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise smtplib.SMTPResponseException(450, b"temporary failure")

    monkeypatch.setattr(mailer, "_send_sync", fail_once)
    monkeypatch.setattr("app.core.mailer.asyncio.sleep", AsyncMock())
    caplog.set_level(logging.WARNING, logger="app.core.mailer")

    await mailer.send_email(OutboundEmail(to="private@example.com", subject="Code", text="Your code is 123456"))

    assert attempts == 2
    assert "private@example.com" not in caplog.text
    assert "123456" not in caplog.text


async def test_permanent_smtp_failure_is_sanitized_and_not_retried(monkeypatch, caplog):
    mailer = Mailer(make_settings())
    attempts = 0

    def reject(_message):
        nonlocal attempts
        attempts += 1
        raise smtplib.SMTPAuthenticationError(535, b"secret app password details")

    monkeypatch.setattr(mailer, "_send_sync", reject)
    caplog.set_level(logging.ERROR, logger="app.core.mailer")

    with pytest.raises(EmailDeliveryError, match="Email delivery failed"):
        await mailer.send_email(OutboundEmail(to="private@example.com", subject="Code", text="Your code is 123456"))

    assert attempts == 1
    assert "private@example.com" not in caplog.text
    assert "123456" not in caplog.text
    assert "secret app password details" not in caplog.text


async def test_background_delivery_failure_is_logged_without_escaping(monkeypatch, caplog):
    mailer = Mailer(make_settings())

    def fail(_message):
        raise OSError("private network details")

    monkeypatch.setattr(mailer, "_send_sync", fail)
    caplog.set_level(logging.ERROR, logger="app.core.mailer")

    await mailer.send_email_in_background(
        OutboundEmail(to="private@example.com", subject="Code", text="Your code is 123456")
    )

    assert "Background email was not delivered" in caplog.text
    assert "private@example.com" not in caplog.text
    assert "123456" not in caplog.text
    assert "private network details" not in caplog.text
