"""Manual end-to-end test: collect -> analyze -> mail, without validation gates.

This is an explicit, user-requested manual path. It deliberately bypasses the
governed finite-validation gates (source-cost proof, cumulative model budget,
price-plan expiry) that block scripts/test_full_chain_once.py. The governed path
and its tests are unchanged and remain the default everywhere else.

One run: at most one Apify run returning at most three items, at most one
DeepSeek request, at most one test email. No ledger, no retry, no scheduling.
Credentials come only from this project's Windows Credential Manager entry and
are never printed. Charges depend on the provider's actual usage and pricing.
"""

from datetime import datetime, timezone
import os
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from monitor.credential_store import CredentialStoreError, load_credentials
from monitor.credentials import ProjectCredentials
from monitor.locking import LockBusy, ProcessLock
from monitor.smtp_transport import SMTPConfig, SMTPNotifier, stdlib_smtp_factory
from scripts import analyze_recovered_three as batch
from scripts.one_shot_source import OneShotSourceError, fetch_three
from scripts.test_full_chain_once import RECIPIENT, SENDER, _body, _notification, _safe
from scripts.verify_api_connections import _build_transport, _valid_secret


SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 465
RUN_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")


class _Failure(ValueError):
    """Fixed public code; no provider body or credential enters an error."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


class _NoLedger:
    """Manual clicks authorize one run without the old cumulative budget gate."""

    def reserve(self, maximum_cny, *, request_sha256=None):
        return None


def manual_test_plan(*, clock=None):
    """Read-only GUI display; never loads credentials or spends money."""
    return {"totals": None, "model_maximum_cny": str(batch.RESERVED_MAXIMUM_CNY),
            "source_run_cap_usd": "0.0009", "credential_channel": "当前用户的 Windows 凭据管理器",
            "sender": SENDER, "recipient": RECIPIENT, "manual": True,
            "ready": True, "reasons": []}


def _directory():
    value = os.environ.get("LOCALAPPDATA", "")
    path = Path(value)
    if not value or not path.is_absolute() or str(path).startswith("\\\\"):
        raise _Failure("MANUAL_TEST_LOCAL_DIRECTORY_REQUIRED")
    return path / "CodexResetMonitor" / "manual-test"


def _path(directory, kind, run_id):
    return directory / (run_id + "." + kind + ".json")


def main(argv=None, *, output=None, clock=None, directory=None, transport=None,
         source_fetch=None, smtp_factory=None, credential_loader=None):
    output = sys.stdout if output is None else output
    argv = sys.argv[1:] if argv is None else argv
    run_id = None
    execute = False
    if argv in ([], ["--preview"], ["--help"]):
        pass
    elif (len(argv) in (3, 4) and argv[0] == "--run" and argv[1] == "--run-id"
          and isinstance(argv[2], str) and RUN_ID.fullmatch(argv[2]) is not None
          and (len(argv) == 3 or argv[3] == "--confirmed")):
        if len(argv) == 3:
            print("FULL_CHAIN_CONFIRMATION_REQUIRED", file=output)
            return 2
        run_id, execute = argv[2], True
    else:
        print("MANUAL_TEST_ARGUMENTS_INVALID", file=output)
        return 2
    print("手动全链路测试：最新最多3条 -> 一次批量分析 -> 一封测试邮件（无预算/证据闸门）", file=output)
    print("收件邮箱：" + RECIPIENT, file=output)
    if not execute:
        print("PREVIEW_ONLY；请在设置窗口点击“新一轮测试”。", file=output)
        return 0
    clock = clock or (lambda: datetime.now(timezone.utc))
    try:
        directory = _directory() if directory is None else Path(directory)
        if _path(directory, "receipt", run_id).exists():
            raise _Failure("FULL_CHAIN_RUN_ALREADY_ATTEMPTED")
        directory.mkdir(parents=True, exist_ok=True)
        load = load_credentials if credential_loader is None else credential_loader
        credentials = load()
        if not isinstance(credentials, ProjectCredentials):
            raise _Failure("PROJECT_CREDENTIAL_INPUT_REQUIRED")
        token, key, password = credentials.apify_token, credentials.deepseek_api_key, credentials.smtp_password
        if not _valid_secret(token) or not _valid_secret(key):
            raise _Failure("MANUAL_TEST_CREDENTIAL_INVALID")
        secrets = (token, key, password)
        transport = _build_transport() if transport is None else transport
        notifier = SMTPNotifier(SMTPConfig(
            host=SMTP_HOST, port=SMTP_PORT, security="SSL", sender=SENDER, user=SENDER,
            recipient=RECIPIENT, password=password, auth_mechanism="LOGIN", timeout_seconds=20),
            smtp_factory=smtp_factory or stdlib_smtp_factory)
        receipt = {"schema_version": 1, "plan_id": run_id, "mode": "MANUAL", "status": "RUNNING"}
        with ProcessLock(directory / "manual-test.lock"):
            if _path(directory, "receipt", run_id).exists():
                raise _Failure("FULL_CHAIN_RUN_ALREADY_ATTEMPTED")

            def checkpoint():
                batch._save(_path(directory, "receipt", run_id), _safe(receipt, secrets))

            try:
                checkpoint()
                print("[1/3] 正在采集最新最多3条…", file=output, flush=True)
                def save_primary(tweets, source):
                    batch._save(_path(directory, "source", run_id), _safe({
                        "schema_version": 1, "plan_id": run_id, "mode": "MANUAL",
                        "status": "CONTEXT_PENDING", "tweets": tweets, "source": source}, secrets))
                    receipt["captured_items"] = len(tweets)
                    checkpoint()

                if source_fetch is None:
                    tweets, source_evidence = fetch_three(token, transport, clock,
                        allow_primary_only=True, source_checkpoint=save_primary)
                else:
                    tweets, source_evidence = source_fetch(token, transport, clock)
                _safe([tweets, source_evidence], secrets)
                if not isinstance(tweets, list) or len(tweets) > 3:
                    raise _Failure("MANUAL_TEST_SOURCE_COUNT_INVALID")
                receipt.update(source=source_evidence, captured_items=len(tweets),
                               context_fallback_items=sum(isinstance(tweet.get("context"), dict)
                                   and tweet["context"].get("context_unavailable") is True for tweet in tweets))
                results, evidence = [], {}
                if tweets:
                    packed, index_to_id = batch._pack(tweets)
                    body = _body(tweets, batch._now(clock))
                    print("[2/3] 正在一次批量分析全部" + str(len(tweets)) + "条…", file=output, flush=True)
                    results = batch._analyze(key, body, transport, evidence, {},
                                             input_ids=[row["id"] for row in packed],
                                             model_budget=_NoLedger())
                    results = batch._restore_ids(results, index_to_id)
                    _safe([results, evidence], secrets)
                else:
                    print("[2/3] 本轮无目标推文，跳过模型请求。", file=output, flush=True)
                receipt.update(analysis=evidence, analyzed_items=len(results))
                notification = _safe(_notification(tweets, results, evidence, batch._now(clock), run_id), secrets)
                batch._save(_path(directory, "result", run_id), {
                    "schema_version": 1, "plan_id": run_id, "mode": "MANUAL", "tweets": tweets,
                    "results": results, "source": source_evidence, "analysis": evidence,
                    "notification": notification})
                print("[3/3] 正在发送一封测试汇总邮件…", file=output, flush=True)
                delivery = notifier.send(notification)
                receipt.update(smtp_status=delivery.status, smtp_error_code=delivery.error_code,
                               status="FULL_CHAIN_OK" if delivery.status == "SENT" and tweets
                               else "FULL_CHAIN_EMPTY" if delivery.status == "SENT" else delivery.status)
                if delivery.error_code:
                    receipt["error_code"] = delivery.error_code
            except (OneShotSourceError, batch._LocalFailure, _Failure) as error:
                receipt.update(status="FAILED", error_code=error.code)
            except (batch.HTTPError, batch._HTTPStatus) as error:
                code = error.code
                receipt.update(status="UNKNOWN", error_code="FULL_CHAIN_HTTP_" +
                               (str(code) if type(code) is int and 100 <= code <= 599 else "ERROR"))
                if isinstance(error, batch.HTTPError):
                    error.close()
            except BaseException:
                receipt.update(status="UNKNOWN", error_code="FULL_CHAIN_REQUEST_UNCERTAIN")
            receipt["completed_at"] = batch._now(clock).isoformat()
            checkpoint()
            status = receipt["status"]
            print(status + " n=" + str(receipt.get("analyzed_items", 0)) +
                  " email=" + receipt.get("smtp_status", "NOT_SENT") +
                  (" " + receipt["error_code"] if receipt.get("error_code") else ""), file=output)
            return 0 if status == "FULL_CHAIN_OK" else 1
    except LockBusy:
        print("FULL_CHAIN_BUSY", file=output)
    except CredentialStoreError as error:
        print(error.code, file=output)
    except _Failure as error:
        print(error.code, file=output)
    except Exception:
        print("MANUAL_TEST_LOCAL_ERROR", file=output)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
