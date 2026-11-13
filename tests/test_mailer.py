import logging
import smtplib
from base64 import b64decode
from unittest.mock import AsyncMock

import httpx
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


def google_script_settings(**overrides):
    values = {
        "mail_transport": "google_apps_script",
        "google_script_url": "https://script.google.com/macros/s/test-deployment/exec",
        "google_script_secret": "s" * 40,
    }
    values.update(overrides)
    return make_settings(**values)


def test_google_apps_script_transport_requires_https_url_and_long_secret():
    with pytest.raises(ValidationError, match="GOOGLE_SCRIPT_URL is required"):
        google_script_settings(google_script_url=None)

    with pytest.raises(ValidationError, match="HTTPS Google Apps Script URL"):
        google_script_settings(google_script_url="http://script.google.com/macros/s/test/exec")

    with pytest.raises(ValidationError, match="Google Apps Script URL"):
        google_script_settings(google_script_url="https://attacker.example/script")

    with pytest.raises(ValidationError, match="at least 32 characters"):
        google_script_settings(google_script_secret="short")


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


async def test_google_apps_script_posts_branded_message_and_attachments(monkeypatch):
    calls = {}

    class Response:
        status_code = 200

        def json(self):
            return {"ok": True, "idempotencyKey": calls["payload"]["idempotencyKey"]}

    class Client:
        def __init__(self, **options):
            calls["options"] = options

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, json):
            calls["url"] = url
            calls["payload"] = json
            return Response()

    monkeypatch.setattr("app.core.mailer.httpx.AsyncClient", Client)
    mailer = Mailer(google_script_settings())
    await mailer.send_email(
        OutboundEmail(
            to="recipient@example.com",
            subject="Report",
            text="Plain text",
            html="<p>HTML</p>",
            attachments=(EmailAttachment("reports\\result.pdf", b"%PDF-1.7", "application/pdf"),),
        )
    )

    payload = calls["payload"]
    assert calls["url"] == "https://script.google.com/macros/s/test-deployment/exec"
    assert calls["options"] == {"timeout": 20, "follow_redirects": True}
    assert payload["secret"] == "s" * 40
    assert payload["to"] == "recipient@example.com" and payload["subject"] == "Report"
    assert payload["text"] == "Plain text\n" and "cid:trustus-health-logo" in payload["html"]
    assert b64decode(payload["inlineImages"]["trustus-health-logo"]["content"]).startswith(b"\x89PNG")
    assert payload["attachments"][0]["filename"] == "result.pdf"
    assert b64decode(payload["attachments"][0]["content"]) == b"%PDF-1.7"


async def test_google_apps_script_retries_timeout_with_same_idempotency_key(monkeypatch, caplog):
    calls = []

    class Response:
        status_code = 200

        def json(self):
            return {"ok": True, "duplicate": True, "idempotencyKey": calls[-1]["idempotencyKey"]}

    class Client:
        def __init__(self, **_options):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, _url, *, json):
            calls.append(json)
            if len(calls) == 1:
                raise httpx.ReadTimeout("private connection detail")
            return Response()

    monkeypatch.setattr("app.core.mailer.httpx.AsyncClient", Client)
    monkeypatch.setattr("app.core.mailer.asyncio.sleep", AsyncMock())
    caplog.set_level(logging.WARNING, logger="app.core.mailer")
    mailer = Mailer(google_script_settings())

    await mailer.send_email(OutboundEmail(to="private@example.com", subject="Code", text="Your code is 123456"))

    assert len(calls) == 2
    assert calls[0]["idempotencyKey"] == calls[1]["idempotencyKey"]
    assert "private connection detail" not in caplog.text
    assert "s" * 40 not in caplog.text
    assert "private@example.com" not in caplog.text
    assert "123456" not in caplog.text


async def test_google_apps_script_rejects_invalid_response_without_retry(monkeypatch):
    attempts = 0

    class Response:
        status_code = 200

        def json(self):
            return []

    class Client:
        def __init__(self, **_options):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            nonlocal attempts
            attempts += 1
            return Response()

    monkeypatch.setattr("app.core.mailer.httpx.AsyncClient", Client)
    with pytest.raises(EmailDeliveryError, match="Email delivery failed"):
        await Mailer(google_script_settings()).send_email(
            OutboundEmail(to="recipient@example.com", subject="Code", text="123456")
        )

    assert attempts == 1


@pytest.mark.parametrize(
    ("statuses", "expected_attempts", "succeeds"),
    [([503, 200], 2, True), ([401], 1, False)],
)
async def test_google_apps_script_retries_server_errors_but_not_client_errors(
    monkeypatch, statuses, expected_attempts, succeeds
):
    calls = []

    class Response:
        def __init__(self, status_code, idempotency_key):
            self.status_code = status_code
            self.idempotency_key = idempotency_key

        def json(self):
            return {"ok": True, "idempotencyKey": self.idempotency_key}

    class Client:
        def __init__(self, **_options):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, _url, *, json):
            calls.append(json)
            return Response(statuses[len(calls) - 1], json["idempotencyKey"])

    monkeypatch.setattr("app.core.mailer.httpx.AsyncClient", Client)
    monkeypatch.setattr("app.core.mailer.asyncio.sleep", AsyncMock())
    mailer = Mailer(google_script_settings())
    email = OutboundEmail(to="recipient@example.com", subject="Code", text="123456")

    if succeeds:
        await mailer.send_email(email)
    else:
        with pytest.raises(EmailDeliveryError, match="Email delivery failed"):
            await mailer.send_email(email)

    assert len(calls) == expected_attempts
    assert len({payload["idempotencyKey"] for payload in calls}) == 1


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
