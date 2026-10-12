"""User-authorized single source -> batch analysis -> test mail run.

Default is a read-only preview. GUI-confirmed new runs use a unique --run-id;
bare --run retains the historical recovery identity. New IDs do not grant budget.
Credentials are loaded only after public approval and cost checks.
"""

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from monitor.local_settings import SENDER as LOCAL_SENDER, RECIPIENT as LOCAL_RECIPIENT
from monitor.locking import LockBusy, ProcessLock
from monitor.classification_prompt import SYSTEM_PROMPT
from monitor.smtp_transport import SMTPConfig, SMTPNotifier, stdlib_smtp_factory
from scripts import analyze_recovered_three as batch
from scripts.verify_api_connections import _build_transport, _valid_secret
from scripts.one_shot_source import OneShotSourceError, fetch_three
from scripts.manage_credentials import get_saved_or_prompt_credentials
from monitor.credentials import CredentialInputError
from monitor.credential_store import CredentialStoreError
from monitor.manual_model_budget import ManualModelBudget, ModelBudgetError


PLAN_ID = "full-chain-20261011-01"
SENDER = LOCAL_SENDER
RECIPIENT = LOCAL_RECIPIENT
SUBJECT = "【ResetMonitor 全链路测试】本轮采集与分析结果"



class _Failure(ValueError):
    def __init__(self, code):
        self.code = code


def _directory():
    value = os.environ.get("LOCALAPPDATA", "")
    path = Path(value)
    if not value or not path.is_absolute() or str(path).startswith("\\\\"):
        raise _Failure("FULL_CHAIN_LOCAL_DIRECTORY_REQUIRED")
    return path / "CodexResetMonitor" / "full-chain-test"


def _path(directory, kind, run_id=PLAN_ID):
    return directory / (run_id + "." + kind + ".json")


def _consumed(directory, run_id=PLAN_ID):
    return any(_path(directory, kind, run_id).exists() or _path(directory, kind, run_id).is_symlink()
               for kind in ("attempt", "receipt", "result"))


def new_run_plan(*, model_budget=None, clock=None):
    """Public, read-only display; never loads credentials or grants allowance."""
    budget = ManualModelBudget() if model_budget is None else model_budget
    totals = None
    reasons = []
    try:
        totals = budget.summary()
        if batch.RESERVED_MAXIMUM_CNY <= 0:
            reasons.append("MODEL_BUDGET_BOUND_UNVERIFIED")
    except ModelBudgetError as error:
        reasons.append(error.code)
    try:
        if not batch._active(batch._now(clock or (lambda: datetime.now(timezone.utc)))):
            reasons.append("FULL_CHAIN_PRICE_PLAN_EXPIRED")
    except (_Failure, batch._LocalFailure) as error:
        reasons.append(error.code)
    return {"totals": totals, "model_maximum_cny": str(batch.RESERVED_MAXIMUM_CNY),
            "source_run_cap_usd": "0.0009", "source_total_verified": False,
            "source_runs": 1, "model_requests": 1, "emails": 1,
            "sender": SENDER, "recipient": RECIPIENT,
            "credential_channel": "当前用户的 Windows 凭据管理器",
            "approval_mode": "per_manual_run", "warnings": ["FULL_CHAIN_SOURCE_COST_UNVERIFIED"],
            "ready": not reasons, "reasons": list(dict.fromkeys(reasons))}


def _safe(value, secrets):
    serialized = json.dumps(value, ensure_ascii=False, allow_nan=False)
    def reflected(item):
        if isinstance(item, str):
            return any(secret in item for secret in secrets)
        if isinstance(item, dict):
            return any(reflected(key) or reflected(child) for key, child in item.items())
        if isinstance(item, (list, tuple)):
            return any(reflected(child) for child in item)
        return False
    if reflected(value) or any(secret in serialized for secret in secrets):
        raise _Failure("FULL_CHAIN_SECRET_REFLECTION")
    return value


def _body(tweets, now):
    packed, _ = batch._pack(tweets)
    body = json.dumps({
        "model": batch.MODEL, "thinking": {"type": "disabled"},
        "response_format": {"type": "json_object"}, "stream": False,
        "temperature": 0, "max_tokens": batch.MAX_OUTPUT_TOKENS,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": json.dumps({
                         "observed_at": now.isoformat(), "timezone": "Asia/Shanghai",
                         "tweets": packed,
                     }, ensure_ascii=False, allow_nan=False)}],
    }, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(body) > batch.MAX_REQUEST_BYTES:
        raise _Failure("FULL_CHAIN_INPUT_TOO_LARGE")
    return body


def _notification(tweets, results, evidence, now, run_id=PLAN_ID, *, background=False,
                  captured_items=None):
    local_time = now.astimezone(timezone(timedelta(hours=8))).isoformat()
    relevant_count = sum(row["label"] in {"POST_RESET", "PRE_RESET", "POSSIBLE_RESET"}
                         for row in results)
    lines = ["ResetMonitor 自动监控" if background else "ResetMonitor 全链路一次性测试", "",
             ("本轮监控已完成，以下保留全部分类结果。是否涉及额外重置，请看每条分析。" if background else
              "本邮件用于验收采集、模型分析与邮件通道。是否确认重置，请看每条分析。"),
             ("采集时间（北京时间）：" if background else "测试时间（北京时间）：") + local_time]
    if background and captured_items is not None:
        lines.append("本轮采集条数：" + str(captured_items))
    lines.append("本轮分析条数：" + str(len(tweets)))
    if background:
        lines.append("涉及额外重置条数：" + str(relevant_count))
    lines.append("")
    labels = {"POST_RESET": "已确认额外重置", "PRE_RESET": "额外重置预告",
              "POSSIBLE_RESET": "疑似额外重置", "IRRELEVANT": "与额外重置无关"}
    by_id = {row["id"]: row for row in results}
    for index, tweet in enumerate(tweets, 1):
        result = by_id[tweet["id"]]
        lines.extend([str(index) + ". " + labels[result["label"]] + "（" + result["label"] + "）",
                      "发布时间：" + tweet["created_at"], "原文：" + tweet["text"],
                      "重置时间：" + (result["reset_time"] or "时间不明"),
                      "分析：" + result["reason"], "链接：" + tweet["url"], ""])
    if not tweets:
        lines.append("本轮采集的推文均已处理，没有新推文，未重复调用模型。"
                     if background and captured_items else
                     "本轮没有可分析的目标推文，模型步骤未执行。")
    if evidence.get("usage_cost_upper_bound_cny"):
        lines.append("按本轮tokens和已核价最高费率计算的模型费用上界（元）："
                     + evidence["usage_cost_upper_bound_cny"])
    subject = SUBJECT
    if background:
        subject = ("【ResetMonitor】Codex 额外重置通告" if relevant_count else
                   "【ResetMonitor】监控汇总：暂无相关公告" if tweets else
                   "【ResetMonitor】监控汇总：暂无新推文" if captured_items else
                   "【ResetMonitor】监控汇总：未采集到推文")
    return {"recipient": RECIPIENT,
            "subject": subject,
            "body": "\n".join(lines),
            "message_id": "<" + run_id + "@qq.com>", "created_at": now.isoformat()}


def main(argv=None, *, output=None, hidden_input=None, clock=None, directory=None,
         transport=None, source_fetch=None, smtp_factory=None,
         credential_loader=None, credential_saver=None, model_budget=None):
    output = sys.stdout if output is None else output
    argv = sys.argv[1:] if argv is None else argv
    new_run = (len(argv) in (3, 4) and argv[:2] == ["--run", "--run-id"]
               and isinstance(argv[2], str)
               and re.fullmatch(r"full-chain-[0-9a-f]{32}", argv[2]) is not None
               and (len(argv) == 3 or argv[3] == "--confirmed"))
    if not new_run and argv not in ([], ["--preview"], ["--help"], ["--run"]):
        print("FULL_CHAIN_ARGUMENTS_INVALID", file=output)
        return 2
    print("一次测试：最新最多3条 -> 一次DeepSeek批量分析 -> 一封测试邮件", file=output)
    print("收件邮箱：" + RECIPIENT, file=output)
    if not new_run and argv != ["--run"]:
        print("PREVIEW_ONLY；新一轮请使用设置窗口；--run 保留为旧轮次兼容入口。", file=output)
        return 0
    run_id = argv[2] if new_run else PLAN_ID
    already_attempted = "FULL_CHAIN_RUN_ALREADY_ATTEMPTED" if new_run else "FULL_CHAIN_ALREADY_ATTEMPTED"
    clock = clock or (lambda: datetime.now(timezone.utc))
    source_fetch = fetch_three if source_fetch is None else source_fetch
    try:
        if new_run and len(argv) != 4:
            raise _Failure("FULL_CHAIN_CONFIRMATION_REQUIRED")
        now = batch._now(clock)
        if not batch._active(now):
            raise _Failure("FULL_CHAIN_PRICE_PLAN_EXPIRED")
        directory = _directory() if directory is None else Path(directory)
        if _consumed(directory, run_id):
            raise _Failure(already_attempted)
        budget = ManualModelBudget() if model_budget is None else model_budget
        try:
            budget.preflight(batch.RESERVED_MAXIMUM_CNY)
        except ModelBudgetError as error:
            raise _Failure(error.code) from None
        print("最多1次Apify运行（运行费用cap USD0.0009）、1次模型请求、1封邮件。", file=output)
        print("本轮模型上界CNY" + str(batch.RESERVED_MAXIMUM_CNY) + "；按确认轮次持久预留，历史费用不清零。"
              if new_run else "模型累计批准预算为CNY1.00；预留必须在调用前通过共享账本校验。", file=output)
        credentials = get_saved_or_prompt_credentials(
            hidden_input=hidden_input, loader=credential_loader, saver=credential_saver, output=output)
        token, key, password = credentials.apify_token, credentials.deepseek_api_key, credentials.smtp_password
        if (not _valid_secret(token) or not _valid_secret(key) or not isinstance(password, str)
                or len(password) != 16 or not password.isascii()
                or any(not 33 <= ord(char) <= 126 for char in password)):
            raise _Failure("FULL_CHAIN_CREDENTIAL_INVALID")
        secrets = (token, key, password)
        notifier = SMTPNotifier(SMTPConfig(
            host="smtp.qq.com", port=465, security="SSL", sender=SENDER, user=SENDER,
            recipient=RECIPIENT, password=password, auth_mechanism="LOGIN", timeout_seconds=15),
            smtp_factory=smtp_factory or stdlib_smtp_factory)
        transport = _build_transport() if transport is None else transport
        with ProcessLock(directory / "full-chain.lock"):
            if _consumed(directory, run_id):
                raise _Failure(already_attempted)
            now = batch._now(clock)
            if not batch._active(now):
                raise _Failure("FULL_CHAIN_PRICE_PLAN_EXPIRED")
            batch._save(_path(directory, "attempt", run_id), {
                "schema_version": 1, "plan_id": run_id, "status": "ATTEMPT_RESERVED",
                "authorized_by": "user_requested_one_full_chain_test",
                "new_run_confirmed": new_run,
                "started_at": now.isoformat(), "maximum_source_runs": 1,
                "maximum_model_requests": 1, "maximum_emails": 1,
                "source_run_charge_cap_usd": "0.0009", "full_source_cost_cap_proven": False,
                "model_reserved_maximum_cny": str(batch.RESERVED_MAXIMUM_CNY),
                "recipient": RECIPIENT, "monitor_baseline_initialized": False,
                "schedule_changed": False,
            }, exclusive=True)
            receipt = {"schema_version": 1, "plan_id": run_id, "status": "RUNNING",
                       "stage": "source", "source_attempts": 0, "model_attempts": 0,
                       "email_attempts": 0, "inbox_delivery_confirmed": False}
            def checkpoint():
                batch._save(_path(directory, "receipt", run_id), _safe(receipt, secrets))
            try:
                receipt["source_attempts"] = 1
                checkpoint()
                print("[1/3] 正在采集最新最多3条…", file=output, flush=True)
                tweets, source_evidence = source_fetch(token, transport, clock)
                _safe([tweets, source_evidence], secrets)
                if not isinstance(tweets, list) or len(tweets) > 3:
                    raise _Failure("FULL_CHAIN_SOURCE_COUNT_INVALID")
                receipt.update(source=source_evidence, captured_items=len(tweets))
                batch._save(_path(directory, "result", run_id), {"tweets": tweets, "stage": "source"})
                results, evidence = [], {}
                if tweets:
                    packed, index_to_id = batch._pack(tweets)
                    body = _body(tweets, batch._now(clock))
                    if not batch._active(batch._now(clock)):
                        raise _Failure("FULL_CHAIN_PRICE_PLAN_EXPIRED")
                    receipt.update(stage="analysis", model_attempts=1)
                    checkpoint()
                    print("[2/3] 正在一次批量分析全部" + str(len(tweets)) + "条…", file=output, flush=True)
                    results = batch._analyze(key, body, transport, evidence, {},
                                             input_ids=[row["id"] for row in packed], model_budget=budget)
                    results = batch._restore_ids(results, index_to_id)
                    _safe([results, evidence], secrets)
                else:
                    print("[2/3] 本轮无目标推文，跳过模型请求。", file=output, flush=True)
                receipt.update(analysis=evidence, analyzed_items=len(results))
                notification = _safe(_notification(tweets, results, evidence, batch._now(clock), run_id), secrets)
                batch._save(_path(directory, "result", run_id), {
                    "schema_version": 1, "plan_id": run_id, "tweets": tweets,
                    "results": results, "source": source_evidence, "analysis": evidence,
                    "notification": notification, "monitor_baseline_initialized": False,
                })
                receipt.update(stage="email", email_attempts=1,
                               message_sha256=hashlib.sha256(notification["body"].encode("utf-8")).hexdigest())
                checkpoint()
                print("[3/3] 正在发送一封测试汇总邮件…", file=output, flush=True)
                delivery = notifier.send(notification)
                receipt.update(smtp_status=delivery.status, smtp_error_code=delivery.error_code,
                               status="FULL_CHAIN_OK" if delivery.status == "SENT" and tweets
                               else "FULL_CHAIN_EMPTY" if delivery.status == "SENT" else delivery.status)
                if delivery.error_code:
                    receipt["error_code"] = delivery.error_code
            except (OneShotSourceError, batch._LocalFailure, _Failure, ModelBudgetError) as error:
                receipt.update(status="FAILED", error_code=error.code)
            except (HTTPError, batch._HTTPStatus) as error:
                code = error.code
                receipt.update(status="UNKNOWN", error_code="FULL_CHAIN_HTTP_" +
                               (str(code) if type(code) is int and 100 <= code <= 599 else "ERROR"))
                if isinstance(error, HTTPError):
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
    except CredentialInputError:
        print("PROJECT_CREDENTIAL_INPUT_REQUIRED", file=output)
    except (KeyboardInterrupt, EOFError):
        print("FULL_CHAIN_CANCELLED", file=output)
    except (_Failure, batch._LocalFailure, ModelBudgetError) as error:
        print(error.code, file=output)
    except Exception:
        print("FULL_CHAIN_LOCAL_ERROR", file=output)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
