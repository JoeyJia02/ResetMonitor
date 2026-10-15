"""Explicit SMTP transport with a durable caller-owned retry policy.

Importing this module and constructing SMTPNotifier never connects to a server.
The caller must supply configuration and a smtplib-compatible factory. The
optional stdlib_smtp_factory is an explicit opt-in; no environment is read.

Protocol references:
https://docs.python.org/3.14/library/smtplib.html
https://github.com/python/cpython/blob/3.14/Lib/smtplib.py
https://www.rfc-editor.org/rfc/rfc5321.html#section-4.2.5
https://www.rfc-editor.org/rfc/rfc5321.html#section-4.5.3.2.6
https://www.rfc-editor.org/rfc/rfc8314.html#section-3.3
https://www.rfc-editor.org/rfc/rfc4954.html#section-6

SENT means the final DATA response was 250, not delivery into an inbox. Once
body transmission starts, an exception or unrecognizable response is UNKNOWN.
No command, AUTH mechanism, or message is retried here. Timeouts are finite per
socket operation; this transport does not claim an end-to-end runtime deadline.
ASCII envelope addresses and PLAIN/LOGIN authentication are supported. MIME
headers and the quoted-printable UTF-8 body do not require SMTPUTF8/8BITMIME.
"""

from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage
from email.utils import format_datetime
import ipaddress
import math
import re
import smtplib
import ssl
from typing import Callable, Protocol

from .models import parse_aware_iso
from .notifier import DeliveryResult


_LOCAL = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+\Z")
_DOMAIN_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
# Only fixed OpenSSL reason names become public diagnostic codes. Never copy
# exception text or an arbitrary reason into receipts, UI or logs.
_TLS_REASONS = {
    "UNEXPECTED_EOF_WHILE_READING": "PEER_CLOSED",
    "WRONG_VERSION_NUMBER": "WRONG_VERSION",
    "TLSV1_ALERT_PROTOCOL_VERSION": "PROTOCOL_VERSION",
    "SSLV3_ALERT_HANDSHAKE_FAILURE": "HANDSHAKE_FAILED",
    "TLSV1_ALERT_INTERNAL_ERROR": "PEER_INTERNAL_ERROR",
    "SSLV3_ALERT_BAD_RECORD_MAC": "BAD_RECORD_MAC",
    "DECRYPTION_FAILED_OR_BAD_RECORD_MAC": "BAD_RECORD_MAC",
    "UNSAFE_LEGACY_RENEGOTIATION_DISABLED": "LEGACY_RENEGOTIATION",
    "DH_KEY_TOO_SMALL": "WEAK_DH_KEY",
    "NO_CIPHERS_AVAILABLE": "NO_CIPHERS",
}


def _domain(value: str) -> bool:
    return bool(value) and len(value) <= 253 and all(
        _DOMAIN_LABEL.fullmatch(label) for label in value.split(".")
    )


def _address(value: object) -> bool:
    if not isinstance(value, str) or not value.isascii() or len(value) > 254:
        return False
    if value.count("@") != 1:
        return False
    local, domain = value.split("@")
    return (
        0 < len(local) <= 64 and _LOCAL.fullmatch(local) is not None
        and not local.startswith(".") and not local.endswith(".") and ".." not in local
        and "." in domain and _domain(domain)
    )


def _header(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and not any(
        ord(character) < 32 or ord(character) == 127 for character in value
    )


@dataclass(frozen=True)
class SMTPConfig:
    """All routing and credentials are supplied explicitly by the caller."""

    host: str
    port: int
    sender: str = field(repr=False)
    user: str = field(repr=False)
    password: str = field(repr=False)
    recipient: str = field(repr=False)
    security: str
    timeout_seconds: float = 30.0
    auth_mechanism: str = "PLAIN"

    def __post_init__(self) -> None:
        valid_host = isinstance(self.host, str) and self.host.isascii()
        if valid_host:
            try:
                ipaddress.ip_address(self.host)
            except ValueError:
                valid_host = _domain(self.host)
        if not valid_host:
            raise ValueError("SMTP_INVALID_HOST")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("SMTP_INVALID_PORT")
        if not _address(self.sender) or not _address(self.recipient):
            raise ValueError("SMTP_SINGLE_ASCII_ADDRESS_REQUIRED")
        for value in (self.user, self.password):
            if not _header(value) or not value.isascii() or len(value) > 4096:
                raise ValueError("SMTP_INVALID_CREDENTIAL")
        if self.security not in ("SSL", "STARTTLS"):
            raise ValueError("SMTP_TLS_REQUIRED")
        if self.auth_mechanism not in ("PLAIN", "LOGIN"):
            raise ValueError("SMTP_UNSUPPORTED_AUTH_MECHANISM")
        if (
            type(self.timeout_seconds) not in (int, float)
            or not 0 < self.timeout_seconds <= 300
            or not math.isfinite(self.timeout_seconds)
        ):
            raise ValueError("SMTP_INVALID_TIMEOUT")


class SMTPSession(Protocol):
    """The standard-library operations used by this adapter."""

    def set_debuglevel(self, level: int) -> None: ...
    def ehlo(self) -> tuple[int, bytes]: ...
    def starttls(self, *, context: ssl.SSLContext) -> tuple[int, bytes]: ...
    def auth(self, mechanism: str, authobject: Callable, *, initial_response_ok: bool) -> tuple[int, bytes]: ...
    def mail(self, sender: str) -> tuple[int, bytes]: ...
    def rcpt(self, recipient: str) -> tuple[int, bytes]: ...
    def docmd(self, command: str) -> tuple[int, bytes]: ...
    def send(self, payload: bytes) -> None: ...
    def getreply(self) -> tuple[int, bytes]: ...
    def quit(self) -> tuple[int, bytes]: ...
    def close(self) -> None: ...


def stdlib_smtp_factory(host: str, port: int, *, timeout: float,
                        context: ssl.SSLContext | None = None) -> SMTPSession:
    """Explicit opt-in factory; invoking it can open a real network connection.

    The notifier passes context only for implicit TLS. STARTTLS is negotiated
    on the returned SMTP session before authentication or message commands.
    """
    if context is not None:
        return smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
    return smtplib.SMTP(host, port, timeout=timeout)


def _reply_code(reply: object) -> int:
    if not isinstance(reply, tuple) or len(reply) != 2:
        raise ValueError("SMTP_INVALID_REPLY")
    code = reply[0]
    if type(code) is not int or not 100 <= code <= 599:
        raise ValueError("SMTP_INVALID_REPLY")
    return code


def _rejected(code: int, phase: str) -> DeliveryResult:
    if 400 <= code <= 599:
        return DeliveryResult("FAILED", retryable=code < 500,
                              error_code=f"SMTP_{phase}_REJECTED")
    return DeliveryResult("FAILED", retryable=False, error_code="SMTP_PROTOCOL_ERROR")


def _auth_callback(config: SMTPConfig) -> Callable:
    # Authenticate one selected mechanism exactly once. SMTP.login() is not
    # used because it may attempt several advertised mechanisms internally.
    challenges = 0

    def respond(challenge=None):
        nonlocal challenges
        challenges += 1
        if config.auth_mechanism == "PLAIN" and challenges == 1:
            return "\0" + config.user + "\0" + config.password
        if config.auth_mechanism == "LOGIN":
            if challenges == 1:
                return config.user
            if challenges == 2:
                return config.password
        raise smtplib.SMTPException("SMTP_AUTH_CHALLENGE_INVALID")

    return respond


def _message(config: SMTPConfig, notification: dict) -> bytes:
    if not isinstance(notification, dict) or notification.get("recipient") != config.recipient:
        raise ValueError("SMTP_RECIPIENT_MISMATCH")
    subject, message_id = notification.get("subject"), notification.get("message_id")
    if not _header(subject) or not _header(message_id):
        raise ValueError("SMTP_INVALID_HEADER")
    if not message_id.startswith("<") or not message_id.endswith(">") or not _address(message_id[1:-1]):
        raise ValueError("SMTP_INVALID_MESSAGE_ID")
    body = notification.get("body")
    if not isinstance(body, str):
        raise ValueError("SMTP_INVALID_BODY")
    created_at = parse_aware_iso(notification.get("created_at"))
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = config.sender
    message["To"] = config.recipient
    message["Subject"] = subject
    message["Message-ID"] = message_id
    message["Date"] = format_datetime(created_at)
    message.set_content(body, charset="utf-8", cte="quoted-printable")
    serialized = message.as_bytes(policy=policy.SMTP)
    # Validate the 7-bit wire contract instead of silently requiring SMTPUTF8.
    serialized.decode("ascii")
    # RFC 5321 4.5.2: dot-stuff every leading period and add exactly one DATA
    # terminator. Do this before entering the uncertainty-sensitive send stage.
    quoted = re.sub(br"(?m)^\.", b"..", serialized)
    if not quoted.endswith(b"\r\n"):
        quoted += b"\r\n"
    return quoted + b".\r\n"


class SMTPNotifier:
    def __init__(self, config: SMTPConfig, *, smtp_factory: Callable):
        if not isinstance(config, SMTPConfig) or not callable(smtp_factory):
            raise TypeError("SMTP_EXPLICIT_CONFIG_AND_FACTORY_REQUIRED")
        self.config = config
        self.smtp_factory = smtp_factory

    def send(self, notification: dict) -> DeliveryResult:
        try:
            payload = _message(self.config, notification)
        except (ValueError, TypeError, KeyError, UnicodeError):
            return DeliveryResult("FAILED", retryable=False, error_code="SMTP_INVALID_MESSAGE")

        session = None
        phase = "CONNECT"
        data_started = False
        accepted = False
        try:
            context = ssl.create_default_context()
            context.check_hostname = True
            context.verify_mode = ssl.CERT_REQUIRED
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            kwargs = {"timeout": self.config.timeout_seconds}
            if self.config.security == "SSL":
                kwargs["context"] = context
            session = self.smtp_factory(self.config.host, self.config.port, **kwargs)
            # Never enable SMTP debug output: it includes credentials and mail.
            session.set_debuglevel(0)
            phase = "EHLO"
            code = _reply_code(session.ehlo())
            if code != 250:
                return _rejected(code, phase)
            if self.config.security == "STARTTLS":
                phase = "STARTTLS"
                code = _reply_code(session.starttls(context=context))
                if code != 220:
                    return _rejected(code, phase)
                phase = "EHLO"
                code = _reply_code(session.ehlo())
                if code != 250:
                    return _rejected(code, phase)
            phase = "AUTH"
            code = _reply_code(session.auth(self.config.auth_mechanism,
                _auth_callback(self.config), initial_response_ok=False))
            if code not in (235, 503):
                return _rejected(code, phase)
            phase = "MAIL"
            code = _reply_code(session.mail(self.config.sender))
            if code != 250:
                return _rejected(code, phase)
            phase = "RCPT"
            code = _reply_code(session.rcpt(self.config.recipient))
            if code not in (250, 251):
                return _rejected(code, phase)
            phase = "DATA_READY"
            code = _reply_code(session.docmd("DATA"))
            if code != 354:
                return _rejected(code, phase)

            # sendall can transmit the full body and terminator before raising.
            # Set this flag BEFORE sending, never infer non-delivery from an
            # exception after this boundary, including locally generated SMTP
            # response exceptions such as an overlong/malformed final reply.
            data_started = True
            session.send(payload)
            code = _reply_code(session.getreply())
            if code == 250:
                accepted = True
                return DeliveryResult("SENT", retryable=False)
            if 400 <= code <= 599:
                return _rejected(code, "DATA")
            return DeliveryResult("UNKNOWN", error_code="SMTP_DATA_UNCERTAIN")
        except Exception as exc:
            if data_started:
                return DeliveryResult("UNKNOWN", error_code="SMTP_DATA_UNCERTAIN")
            if isinstance(exc, ssl.SSLCertVerificationError):
                return DeliveryResult("FAILED", retryable=False,
                                      error_code=f"SMTP_{phase}_TLS_CERTIFICATE_INVALID")
            if isinstance(exc, smtplib.SMTPResponseException):
                code = getattr(exc, "smtp_code", None)
                if type(code) is int and 400 <= code <= 599:
                    return _rejected(code, phase)
                return DeliveryResult("FAILED", retryable=False, error_code="SMTP_PROTOCOL_ERROR")
            if isinstance(exc, ssl.SSLError):
                reason = getattr(exc, "reason", None)
                detail = _TLS_REASONS.get(reason, "ERROR") if isinstance(reason, str) else "ERROR"
                return DeliveryResult("FAILED", retryable=False,
                                      error_code=f"SMTP_{phase}_TLS_{detail}")
            if isinstance(exc, smtplib.SMTPNotSupportedError):
                return DeliveryResult("FAILED", retryable=False,
                                      error_code=f"SMTP_{phase}_UNSUPPORTED")
            retryable = isinstance(exc, (OSError, smtplib.SMTPServerDisconnected))
            return DeliveryResult("FAILED", retryable=retryable, error_code=f"SMTP_{phase}_UNAVAILABLE")
        finally:
            if session is not None:
                # Never issue commands into an uncertain DATA stream. After a
                # confirmed 250, QUIT/close errors cannot change SENT.
                if not data_started or accepted:
                    try:
                        session.quit()
                    except Exception:
                        pass
                try:
                    session.close()
                except Exception:
                    pass
