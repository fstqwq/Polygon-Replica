import base64
import os
import smtplib
import ssl
from collections.abc import Iterator
from contextlib import contextmanager
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from unittest.mock import patch

from tests.common import E2ETestBase
from tests.db_helpers import db_fetch_one
from tests.ui_support import (
    UIHelpersMixin,
    runtime,
)
from app.service.platform.secret_box import ENCRYPTION_KEY_ENV, SecretBox, SecretBoxDecryptError


_KEY = base64.urlsafe_b64encode(b"0" * 32).decode("ascii").rstrip("=")
_WRONG_KEY = base64.urlsafe_b64encode(b"1" * 32).decode("ascii").rstrip("=")


class _SmtpMailbox:
    """External SMTP peer requiring TLS and login before accepting mail."""

    def __init__(self) -> None:
        self.secure = False
        self.authenticated = False
        self.messages: list[bytes] = []

    def ehlo(self) -> tuple[int, bytes]:
        return 250, b"mailbox ready"

    def starttls(self, *, context: ssl.SSLContext) -> tuple[int, bytes]:
        if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
            raise smtplib.SMTPException("certificate validation required")
        self.secure = True
        return 220, b"TLS ready"

    def login(self, user: str, password: str) -> tuple[int, bytes]:
        if not self.secure:
            raise smtplib.SMTPException("TLS required")
        if (user, password) != ("mailer@example.com", "secret-token"):
            raise smtplib.SMTPAuthenticationError(535, b"invalid credentials")
        self.authenticated = True
        return 235, b"authenticated"

    def send_message(self, message: EmailMessage) -> dict[str, tuple[int, bytes]]:
        if not self.authenticated:
            raise smtplib.SMTPException("authentication required")
        self.messages.append(message.as_bytes(policy=policy.SMTP))
        return {}

    @contextmanager
    def connect(self, host: str, port: int, *, timeout: float) -> Iterator[_SmtpMailbox]:
        if (host, port) != ("smtp.example.com", 587) or timeout <= 0:
            raise OSError("mailbox unavailable")
        yield self


class TestSmtpConfig(UIHelpersMixin, E2ETestBase):
    seed_primary_workspace = True
    seed_default_workspace = False

    def test_secret_box_round_trip_uses_envelope_ciphertext(self) -> None:
        with patch.dict(os.environ, {ENCRYPTION_KEY_ENV: _KEY}):
            box = SecretBox.from_environment()
            first = box.encrypt_text("smtp-password", aad=b"test")
            second = box.encrypt_text("smtp-password", aad=b"test")

        self.assertTrue(first.startswith("enc:v1:aes-256-gcm:"))
        self.assertNotEqual(first, second)
        self.assertNotIn("smtp-password", first)
        with patch.dict(os.environ, {ENCRYPTION_KEY_ENV: _KEY}):
            self.assertEqual(
                SecretBox.from_environment().decrypt_text(first, aad=b"test"),
                "smtp-password",
            )
        with patch.dict(os.environ, {ENCRYPTION_KEY_ENV: _WRONG_KEY}):
            with self.assertRaises(SecretBoxDecryptError):
                SecretBox.from_environment().decrypt_text(first, aad=b"test")

    def test_smtp_store_encrypts_password_and_preserves_blank_password(self) -> None:
        actor = db_fetch_one("SELECT id FROM users WHERE username=?", [self.user])
        self.assertIsNotNone(actor)
        with patch.dict(os.environ, {ENCRYPTION_KEY_ENV: _KEY}):
            runtime.smtp_config_service.save_from_form(
                host="smtp.example.com",
                port="587",
                username="mailer@example.com",
                password="secret-token",
                clear_password=False,
                actor_user_id=int(actor["id"]),
            )
            row = db_fetch_one("SELECT * FROM smtp_config WHERE id=1")
            self.assertIsNotNone(row)
            ciphertext = str(row["password_ciphertext"])
            self.assertNotIn("secret-token", ciphertext)
            self.assertEqual(runtime.smtp_config_service.credentials().password, "secret-token")

            runtime.smtp_config_service.save_from_form(
                host="smtp2.example.com",
                port="465",
                username="mailer@example.com",
                password="",
                clear_password=False,
                actor_user_id=int(actor["id"]),
            )
            kept = db_fetch_one("SELECT host,password_ciphertext FROM smtp_config WHERE id=1")
            self.assertIsNotNone(kept)
            self.assertEqual(str(kept["host"]), "smtp2.example.com")
            self.assertEqual(str(kept["password_ciphertext"]), ciphertext)

            runtime.smtp_config_service.save_from_form(
                host="smtp2.example.com",
                port="465",
                username="mailer@example.com",
                password="ignored",
                clear_password=True,
                actor_user_id=int(actor["id"]),
            )
            cleared = db_fetch_one("SELECT password_ciphertext FROM smtp_config WHERE id=1")
            self.assertIsNotNone(cleared)
            self.assertEqual(str(cleared["password_ciphertext"]), "")

    def test_mail_delivery_requires_tls_and_authentication(self) -> None:
        actor = db_fetch_one("SELECT id FROM users WHERE username=?", [self.user])
        self.assertIsNotNone(actor)
        with patch.dict(os.environ, {ENCRYPTION_KEY_ENV: _KEY}):
            runtime.smtp_config_service.save_from_form(
                host="smtp.example.com",
                port="587",
                username="mailer@example.com",
                password="secret-token",
                clear_password=False,
                actor_user_id=int(actor["id"]),
            )
            for registration in (False, True):
                with self.subTest(registration=registration):
                    mailbox = _SmtpMailbox()
                    with patch("app.service.mail.smtp_config.smtplib.SMTP", mailbox.connect):
                        if registration:
                            runtime.smtp_config_service.send_registration_email(
                                recipient="user@example.com",
                                verification_code="8F3K-2Q7M-Z9PA",
                                expires_in_sec=1800,
                            )
                        else:
                            runtime.smtp_config_service.send_test_email(recipient="user@example.com")
                    self.assertEqual(len(mailbox.messages), 1)
                    message = BytesParser(policy=policy.default).parsebytes(mailbox.messages[0])
                    self.assertEqual(message["From"], "mailer@example.com")
                    self.assertEqual(message["To"], "user@example.com")
                    if registration:
                        expected_body = (
                            "Confirm your Polygon-Replica registration with this verification code:\n\n"
                            "8F3K-2Q7M-Z9PA\n\n"
                            "This code expires in 30 minutes.\n\n"
                            "If you did not request this account, ignore this email.\n"
                        )
                    else:
                        expected_body = "This is a Polygon-Replica SMTP test email.\n"
                    self.assertEqual(message.get_content().replace("\r\n", "\n"), expected_body)
