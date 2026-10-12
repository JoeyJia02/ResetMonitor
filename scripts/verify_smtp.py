"""Manual, one-attempt SMTP setup check; default execution is a read-only preview.

This is independent of the paid source/model validation CLI. It does not load
API credentials, change the monitor state, or enable recurring notifications.
"""

from datetime import datetime, timezone
import getpass
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import warnings

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from monitor.local_settings import SENDER as LOCAL_SENDER, RECIPIENT as LOCAL_RECIPIENT
from monitor.locking import LockBusy, ProcessLock
from monitor.smtp_transport import SMTPConfig, SMTPNotifier, stdlib_smtp_factory


PLAN_ID = "smtp-setup-20261011-01"
EXPIRES_AT = datetime(2026, 10, 12, 7, 36, tzinfo=timezone.utc)
SENDER = LOCAL_SENDER
RECIPIENT = LOCAL_RECIPIENT
SUBJECT = "【ResetMonitor 测试】邮件连通检查"
BODY = (
    "这是一封 Codex Reset Monitor 邮件通道测试邮件。\n\n"
    "用途：确认 QQ 邮箱能够将通知发送到指定的 Gmail 收件箱。\n"
    "本邮件不表示 Codex 额度已经重置。\n"
    "收到后，请在项目对话中回复“测试邮件已收到”。\n"
)
CONFIRMATION = "SEND ONE TEST"


def _now(clock):
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("SMTP_TEST_CLOCK_INVALID")
    return value.astimezone(timezone.utc)


def _default_directory():
    # Only consulted during explicit send mode; no service credentials are read.
    value = os.environ.get("LOCALAPPDATA", "")
    root = Path(value)
    if not value or not root.is_absolute() or str(root).startswith("\\\\"):
        raise ValueError("SMTP_TEST_LOCAL_DIRECTORY_REQUIRED")
    return root / "CodexResetMonitor" / "smtp-setup-test"


def _consumed(directory):
    return any(path.exists() or path.is_symlink() for path in (
        directory / (PLAN_ID + ".attempt.json"),
        directory / (PLAN_ID + ".receipt.json"),
    ))


def _reserve(directory, approved_at):
    record = {
        "schema_version": 1, "plan_id": PLAN_ID, "status": "ATTEMPT_RESERVED",
        "approved_at": approved_at.isoformat(), "expires_at": EXPIRES_AT.isoformat(),
        "sender": SENDER, "recipient": RECIPIENT, "subject": SUBJECT,
        "body_sha256": hashlib.sha256(BODY.encode("utf-8")).hexdigest(),
        "max_attempts": 1, "reserved_attempts": 1,
        "approval_method": "local_tty_SEND_ONE_TEST",
    }
    # Exclusive creation prevents an existing attempt from being overwritten.
    # A partial/failed write also leaves a conservative hold for the next run.
    with (directory / (PLAN_ID + ".attempt.json")).open("x", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=True)
        handle.flush()
        os.fsync(handle.fileno())


def _write_receipt(directory, status, error_code, completed_at):
    record = {
        "schema_version": 1, "plan_id": PLAN_ID, "status": status,
        "error_code": error_code, "completed_at": completed_at.isoformat(),
        "attempts": 1, "inbox_delivery_confirmed": False,
    }
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                         prefix=PLAN_ID + ".", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(record, handle, ensure_ascii=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, directory / (PLAN_ID + ".receipt.json"))
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _preview(output):
    print("SMTP SETUP TEST - ONE ATTEMPT", file=output)
    print("From: " + SENDER, file=output)
    print("To: " + RECIPIENT, file=output)
    print("Subject: " + SUBJECT, file=output)
    print("Body:\n" + BODY, file=output)
    print("Server: smtp.qq.com:465 / SSL / LOGIN", file=output)
    print("Expires: 2026-10-12 15:36 Asia/Shanghai", file=output)


def main(argv=None, *, output=None, confirm=None, hidden_input=None,
         clock=None, directory=None, smtp_factory=None):
    """Injected dependencies are for offline tests, never CLI switches."""
    output = sys.stdout if output is None else output
    argv = sys.argv[1:] if argv is None else argv
    if argv not in ([], ["--preview"], ["--help"], ["--send-one-test"]):
        print("SMTP_TEST_ARGUMENTS_INVALID", file=output)
        return 2
    _preview(output)
    if argv != ["--send-one-test"]:
        print("PREVIEW_ONLY", file=output)
        return 0

    clock = clock or (lambda: datetime.now(timezone.utc))
    try:
        if _now(clock) >= EXPIRES_AT:
            print("SMTP_TEST_PLAN_EXPIRED", file=output)
            return 2
        directory = _default_directory() if directory is None else Path(directory)
        if _consumed(directory):
            print("SMTP_TEST_ALREADY_RESERVED", file=output)
            return 2
        if confirm is None or hidden_input is None:
            if not sys.stdin.isatty() or not sys.stderr.isatty():
                print("SMTP_TEST_INTERACTIVE_TERMINAL_REQUIRED", file=output)
                return 2
        confirm = input if confirm is None else confirm
        hidden_input = getpass.getpass if hidden_input is None else hidden_input
        if confirm("To authorize this single attempt, type SEND ONE TEST: ") != CONFIRMATION:
            print("SMTP_TEST_CANCELLED", file=output)
            return 2
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = hidden_input("QQ SMTP authorization code (hidden): ")
        if (not isinstance(password, str) or len(password) != 16 or not password.isascii()
                or any(not 33 <= ord(character) <= 126 for character in password)):
            print("SMTP_TEST_AUTHORIZATION_CODE_INVALID", file=output)
            return 2

        with ProcessLock(directory / (PLAN_ID + ".lock")):
            if _consumed(directory):
                print("SMTP_TEST_ALREADY_RESERVED", file=output)
                return 2
            approved_at = _now(clock)
            if approved_at >= EXPIRES_AT:
                print("SMTP_TEST_PLAN_EXPIRED", file=output)
                return 2
            config = SMTPConfig(host="smtp.qq.com", port=465, security="SSL",
                                sender=SENDER, recipient=RECIPIENT, user=SENDER,
                                password=password, auth_mechanism="LOGIN", timeout_seconds=15)
            notifier = SMTPNotifier(config, smtp_factory=smtp_factory or stdlib_smtp_factory)
            notification = {"recipient": RECIPIENT, "subject": SUBJECT, "body": BODY,
                            "message_id": "<" + PLAN_ID + "@qq.com>",
                            "created_at": approved_at.isoformat()}
            _reserve(directory, approved_at)
            # No automatic retry, refund, reset, or new approval is generated.
            try:
                if _now(clock) >= EXPIRES_AT:
                    print("SMTP_TEST_PLAN_EXPIRED_AFTER_RESERVATION", file=output)
                    return 2
                result = notifier.send(notification)
            except BaseException:
                _write_receipt(directory, "UNKNOWN", "SMTP_TEST_INTERRUPTED", _now(clock))
                print("UNKNOWN SMTP_TEST_INTERRUPTED", file=output)
                return 1
            try:
                _write_receipt(directory, result.status, result.error_code, _now(clock))
            except Exception:
                print("SMTP_TEST_RECEIPT_WRITE_FAILED", file=output)
                return 1
            print(result.status + (" " + result.error_code if result.error_code else ""), file=output)
            return 0 if result.status == "SENT" else 1
    except LockBusy:
        print("SMTP_TEST_BUSY", file=output)
        return 2
    except (KeyboardInterrupt, EOFError):
        print("SMTP_TEST_CANCELLED", file=output)
        return 2
    except Exception:
        # Neither supplied values nor SMTP/provider exception text are printed.
        print("SMTP_TEST_LOCAL_ERROR", file=output)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
