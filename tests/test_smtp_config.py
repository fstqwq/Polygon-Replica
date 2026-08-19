import base64
import os
from unittest.mock import MagicMock, patch

from tests.common import E2ETestBase
from tests.db_helpers import db_fetch_one
from tests.ui_support import (
    UIHelpersMixin,
    runtime,
)
from app.service.platform.secret_box import ENCRYPTION_KEY_ENV, SecretBox, SecretBoxDecryptError


_KEY = base64.urlsafe_b64encode(b"0" * 32).decode("ascii").rstrip("=")
_WRONG_KEY = base64.urlsafe_b64encode(b"1" * 32).decode("ascii").rstrip("=")


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

    def test_smtp_test_email_uses_starttls_for_587(self) -> None:
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
            smtp_context = MagicMock()
            smtp = smtp_context.__enter__.return_value
            with patch("app.service.mail.smtp_config.smtplib.SMTP", return_value=smtp_context):
                runtime.smtp_config_service.send_test_email(recipient="admin@example.com")

        smtp.ehlo.assert_called()
        smtp.starttls.assert_called_once()
        smtp.login.assert_called_once_with("mailer@example.com", "secret-token")
        smtp.send_message.assert_called_once()

    def test_registration_email_sends_code_without_verification_link(self) -> None:
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
            smtp_context = MagicMock()
            smtp = smtp_context.__enter__.return_value
            with patch("app.service.mail.smtp_config.smtplib.SMTP", return_value=smtp_context):
                runtime.smtp_config_service.send_registration_email(
                    recipient="user@example.com",
                    verification_code="8F3K-2Q7M-Z9PA",
                    expires_in_sec=1800,
                )

        smtp.send_message.assert_called_once()
        message = smtp.send_message.call_args.args[0]
        body = message.get_content()
        self.assertIn("8F3K-2Q7M-Z9PA", body)
        self.assertIn("This code expires in 30 minutes.", body)
        self.assertNotIn("http://", body)
        self.assertNotIn("https://", body)
        self.assertNotIn("/register/verify", body)
        self.assertNotIn("token=", body)
