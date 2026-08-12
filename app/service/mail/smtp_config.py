"""SMTP configuration and test-mail sending service."""

import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import parseaddr

from app.db import DB
from app.main_util import form_text
from app.service.disk.smtp_config_store import SmtpConfigStore
from app.service.platform.secret_box import SecretBox, SecretBoxConfigError, SecretBoxDecryptError


_SMTP_PASSWORD_AAD = b"polygon-replica:smtp-password:v1"
_SMTP_TIMEOUT_SEC = 10.0
_MAX_HOST_LEN = 255
_MAX_USERNAME_LEN = 320
_MAX_PASSWORD_BYTES = 4096


@dataclass(frozen=True)
class SmtpConfigSnapshot:
    host: str
    port: int
    username: str
    password_configured: bool
    encryption_key_ready: bool
    encryption_key_label: str
    encryption_key_message: str
    security_mode: str
    updated_at: str


@dataclass(frozen=True)
class SmtpCredentials:
    host: str
    port: int
    username: str
    password: str


def smtp_security_mode_for_port(port: int) -> str:
    if int(port) == 465:
        return "SSL"
    if int(port) == 587:
        return "STARTTLS"
    return "Plain SMTP"


def _format_expiry_text(expires_in_sec: int) -> str:
    safe_seconds = max(1, int(expires_in_sec))
    if safe_seconds % 3600 == 0:
        hours = safe_seconds // 3600
        return f"{hours} hour{'s' if hours != 1 else ''}"
    if safe_seconds % 60 == 0:
        minutes = safe_seconds // 60
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{safe_seconds} seconds"


def _reject_control_text(value: str, *, label: str) -> str:
    if any(ch in value for ch in ("\x00", "\r", "\n")):
        raise ValueError(f"{label} must not contain control characters")
    return value


def _normalize_host(value: object) -> str:
    host = _reject_control_text(form_text(value).strip(), label="SMTP host")
    if len(host) > _MAX_HOST_LEN:
        raise ValueError("SMTP host is too long")
    if any(ch.isspace() for ch in host):
        raise ValueError("SMTP host must not contain whitespace")
    return host


def _normalize_username(value: object) -> str:
    username = _reject_control_text(form_text(value).strip(), label="SMTP username")
    if len(username) > _MAX_USERNAME_LEN:
        raise ValueError("SMTP username is too long")
    return username


def _normalize_password(value: object) -> str:
    password = _reject_control_text(form_text(value), label="SMTP password")
    if len(password.encode("utf-8")) > _MAX_PASSWORD_BYTES:
        raise ValueError("SMTP password is too long")
    return password


def _normalize_port(value: object) -> int:
    raw = form_text(value).strip()
    try:
        port = int(raw)
    except Exception as exc:
        raise ValueError("SMTP port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("SMTP port must be between 1 and 65535")
    return port


def _normalize_recipient(value: object) -> str:
    raw = _reject_control_text(form_text(value).strip(), label="test recipient")
    _, addr = parseaddr(raw)
    if not addr or "@" not in addr:
        raise ValueError("test recipient must be an email address")
    return addr


class SmtpConfigService:
    def __init__(self, db: DB):
        self._store = SmtpConfigStore(db)

    def snapshot(self) -> SmtpConfigSnapshot:
        row = self._store.get()
        key_status = SecretBox.environment_status()
        return SmtpConfigSnapshot(
            host=row.host,
            port=row.port,
            username=row.username,
            password_configured=bool(row.password_ciphertext),
            encryption_key_ready=key_status.ready,
            encryption_key_label=key_status.label,
            encryption_key_message=key_status.message,
            security_mode=smtp_security_mode_for_port(row.port),
            updated_at=row.updated_at,
        )

    def save_from_form(
        self,
        *,
        host: object,
        port: object,
        username: object,
        password: object,
        clear_password: bool,
        actor_user_id: int,
    ) -> None:
        stored = self._store.get()
        normalized_host = _normalize_host(host)
        normalized_port = _normalize_port(port)
        normalized_username = _normalize_username(username)
        normalized_password = _normalize_password(password)
        password_ciphertext = stored.password_ciphertext
        if clear_password:
            password_ciphertext = ""
        elif normalized_password:
            password_ciphertext = SecretBox.from_environment().encrypt_text(
                normalized_password,
                aad=_SMTP_PASSWORD_AAD,
            )
        self._store.save(
            host=normalized_host,
            port=normalized_port,
            username=normalized_username,
            password_ciphertext=password_ciphertext,
            actor_user_id=int(actor_user_id),
        )

    def credentials(self) -> SmtpCredentials:
        row = self._store.get()
        if not row.host:
            raise ValueError("SMTP host is not configured")
        if not row.username:
            raise ValueError("SMTP username is not configured")
        if not row.password_ciphertext:
            raise ValueError("SMTP password is not configured")
        try:
            password = SecretBox.from_environment().decrypt_text(
                row.password_ciphertext,
                aad=_SMTP_PASSWORD_AAD,
            )
        except SecretBoxConfigError as exc:
            raise ValueError(str(exc)) from exc
        except SecretBoxDecryptError as exc:
            raise ValueError("SMTP password cannot be decrypted; re-enter it") from exc
        return SmtpCredentials(row.host, row.port, row.username, password)

    def delivery_configured(self) -> bool:
        row = self._store.get()
        return bool(row.host and row.username and row.password_ciphertext)

    def send_registration_email(
        self,
        *,
        recipient: object,
        verification_code: str,
        expires_in_sec: int,
    ) -> None:
        safe_recipient = _normalize_recipient(recipient)
        safe_code = _reject_control_text(form_text(verification_code).strip(), label="verification code")
        if not safe_code:
            raise ValueError("verification code is required")
        expiry_text = _format_expiry_text(expires_in_sec)
        credentials = self.credentials()
        sender = (
            credentials.username
            if "@" in credentials.username
            else "polygon-replica@localhost"
        )
        message = EmailMessage()
        message["Subject"] = "Polygon-Replica email verification"
        message["From"] = sender
        message["To"] = safe_recipient
        message.set_content(
            "Confirm your Polygon-Replica registration with this verification code:\n\n"
            f"{safe_code}\n\n"
            f"This code expires in {expiry_text}.\n\n"
            "If you did not request this account, ignore this email.\n"
        )
        try:
            self._send_message(credentials, message)
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"registration email failed: {exc}") from exc

    def send_test_email(self, *, recipient: object) -> None:
        safe_recipient = _normalize_recipient(recipient)
        credentials = self.credentials()
        sender = (
            credentials.username
            if "@" in credentials.username
            else "polygon-replica@localhost"
        )
        message = EmailMessage()
        message["Subject"] = "Polygon-Replica SMTP test"
        message["From"] = sender
        message["To"] = safe_recipient
        message.set_content("This is a Polygon-Replica SMTP test email.\n")
        try:
            self._send_message(credentials, message)
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"SMTP test email failed: {exc}") from exc

    def _send_message(self, credentials: SmtpCredentials, message: EmailMessage) -> None:
        mode = smtp_security_mode_for_port(credentials.port)
        if mode == "SSL":
            with smtplib.SMTP_SSL(
                credentials.host,
                credentials.port,
                timeout=_SMTP_TIMEOUT_SEC,
            ) as smtp:
                smtp.login(credentials.username, credentials.password)
                smtp.send_message(message)
            return
        with smtplib.SMTP(credentials.host, credentials.port, timeout=_SMTP_TIMEOUT_SEC) as smtp:
            if mode == "STARTTLS":
                smtp.ehlo()
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
            smtp.login(credentials.username, credentials.password)
            smtp.send_message(message)
