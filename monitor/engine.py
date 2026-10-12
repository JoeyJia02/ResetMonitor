"""One bounded, recoverable round, with injected service interfaces only."""
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import time
from uuid import uuid4

from config import Config
from .classifier import BudgetedClassifier, Classifier, ClassificationError, parse_classification
from .locking import LockBusy, ProcessLock
from .models import DurableFetcher, Fetcher, SourceContractError, StageFailure, parse_time, timestamp, validate_snapshot
from .notifier import DeliveryResult, Notifier, build_failure_notification, build_notification
from .state import StateError, StateStore, initial_state


@dataclass
class RunResult:
    status: str
    ingested: int = 0
    classified: int = 0
    sent: int = 0
    errors: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)


class Monitor:
    def __init__(self, config: Config, store: StateStore, fetcher: Fetcher,
                 classifier: Classifier, notifier: Notifier, *, clock=None,
                 sleep=time.sleep, fault_hook=None, monotonic=time.monotonic,
                 test_notifications=False):
        self.config, self.store = config, store
        self.fetcher, self.classifier, self.notifier = fetcher, classifier, notifier
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep
        self.monotonic = monotonic
        self.fault_hook = fault_hook or (lambda event, state: None)
        if type(test_notifications) is not bool:
            raise ValueError("INVALID_TEST_NOTIFICATION_MODE")
        self.test_notifications = test_notifications

    def _now(self):
        return parse_time(timestamp(self.clock()))

    def _save(self, state, event=None):
        self.store.save(state)
        if event:
            self.fault_hook(event, deepcopy(state))

    def run(self, *, dry_run=False, recover_state=False, upgrade_requests=(), resume_paused=False):
        # A dry run is an inspection, not an in-memory delivery simulation.
        # It does not even create/open a lock file or invoke service interfaces.
        if dry_run:
            state = self.store.load(recover=False)
            return RunResult("DRY_RUN", details={
                "initialized": bool(state and state["initialized"]),
                "execution_mode": state["execution_mode"] if state else self.config.execution_mode,
                "queued_classifications": len(state["inbox"]) if state else 0,
                "unsent_notifications": sum(n["status"] != "SENT" for n in state["pending_notifications"].values()) if state else 0,
                "side_effects": False,
            })
        try:
            with ProcessLock(str(self.config.state_path) + ".lock"):
                return self._run_locked(recover_state, upgrade_requests, resume_paused)
        except LockBusy:
            return RunResult("LOCKED", details={"skipped": True})

    def _run_locked(self, recover_state, upgrade_requests, resume_paused):
        state = self.store.load(recover=recover_state)
        if state is None:
            state = initial_state(self.config.target_username, execution_mode=self.config.execution_mode,
                                  target_user_id=self.config.target_user_id)
        if state["target"] != self.config.target_username:
            raise StateError("Configured target differs from persisted target")
        if state["execution_mode"] != self.config.execution_mode:
            raise StateError("State execution origin differs from configured mode")
        if state["target_user_id"] != self.config.target_user_id:
            raise StateError("State stable author identity differs from configured identity")
        if getattr(self.store, "recovered_from_backup", False):
            # Recovered old data cannot prove what was sent after that backup.
            state["recovery_required"] = True
            self._save(state)
            return RunResult("RECOVERY_REQUIRED", errors=["BACKUP_ROLLBACK_REVIEW_REQUIRED"])
        if state.get("recovery_required"):
            return RunResult("RECOVERY_REQUIRED", errors=["BACKUP_ROLLBACK_REVIEW_REQUIRED"])
        if state["active_fetch"] is not None and not isinstance(self.fetcher, DurableFetcher):
            raise StateError("An active paid fetch requires its durable source adapter")
        if isinstance(self.fetcher, DurableFetcher):
            self.fetcher.bind_state(state, self._save)
        result = RunResult("OK")
        self._recover_sending(state)
        if resume_paused:
            self._resume_confirmed_failures(state)
        fetch_error = None
        if state["failed_items"].get("fetch", {}).get("retryable") is False:
            fetch_error = "FETCH_PAUSED"
        elif not self._due(state["failed_items"].get("fetch", {}).get("next_retry_at")):
            fetch_error = "FETCH_COOLDOWN"
        else:
            try:
                raw = self.fetcher.fetch_latest(limit=self.config.fetch_limit)
                tweets = validate_snapshot(raw, self.config.target_username, self.config.fetch_limit,
                                           target_user_id=self.config.target_user_id)
            except Exception as exc:
                fetch_error = "SOURCE_CONTRACT_ERROR" if isinstance(exc, SourceContractError) else "FETCH_FAILED"
                retryable = not isinstance(exc, StageFailure) or exc.retryable
                retry_at = None
                if isinstance(exc, StageFailure) and exc.retry_after_seconds is not None:
                    try:
                        retry_at = timestamp(self._now() + timedelta(seconds=exc.retry_after_seconds))
                    except (OverflowError, ValueError):
                        retryable = False
                state["failed_items"]["fetch"] = {"stage": "FETCH", "error_code": fetch_error,
                                                   "retryable": retryable, "next_retry_at": retry_at}
                self._save(state)
            else:
                state["failed_items"].pop("fetch", None)
                self._ingest(state, tweets, result, raw)
        self._queue_upgrades(state, upgrade_requests)
        self._classify(state, result)
        self._deliver(state, result)
        pending = bool(state["inbox"]) or any(n["status"] != "SENT" for n in state["pending_notifications"].values())
        error = fetch_error or ("UNFINISHED_ITEMS" if pending else None)
        incident_key = self._health(state, error)
        if incident_key:
            self._deliver(state, result, only_key=incident_key)
        result.status = "DEGRADED" if error else "OK"
        if error:
            result.errors.append(error)
        result.details = {"initialized": state["initialized"], "last_seen_id": state["last_seen_id"],
                          "queued_classifications": len(state["inbox"]),
                          "unsent_notifications": sum(n["status"] != "SENT" for n in state["pending_notifications"].values())}
        return result

    def _resume_confirmed_failures(self, state):
        """Explicit local operator action; never reset uncertainty resend budgets."""
        if state["failed_items"].get("fetch", {}).get("retryable") is False:
            state["failed_items"].pop("fetch")
        for tweet_id, entry in state["inbox"].items():
            if entry.get("retryable") is False:
                entry.update(retryable=True, next_retry_at=None)
                state["failed_items"].pop(f"classify:{tweet_id}", None)
        for key, notice in state["pending_notifications"].items():
            if notice["status"] == "FAILED" and not notice["retryable"] and not notice["had_unknown"]:
                notice.update(retryable=True, needs_attention=False, next_retry_at=None)
                state["failed_items"].pop(f"notify:{key}", None)
        self._save(state)

    def _ingest(self, state, tweets, result, snapshot):
        now = timestamp(self._now())
        state["last_fetch_at"] = now
        if not state["initialized"]:
            if tweets:
                baseline = max(tweets, key=lambda t: int(t["id"]))["id"]
                state.update(initialized=True, initial_baseline_id=baseline, last_seen_id=baseline)
        else:
            for tweet in tweets:
                tweet_id = tweet["id"]
                if int(tweet_id) <= int(state["initial_baseline_id"]):
                    continue
                if tweet_id not in state["processed_ids"] and tweet_id not in state["inbox"]:
                    state["inbox"][tweet_id] = {"tweet": tweet, "attempts": 0, "retryable": True,
                                               "next_retry_at": None, "last_error": None}
                    result.ingested += 1
            if tweets:
                state["last_seen_id"] = str(max(int(state["last_seen_id"]), *(int(t["id"]) for t in tweets)))
        # READY clearing, the raw inbox and watermark are a single atomic commit.
        if isinstance(self.fetcher, DurableFetcher):
            state["last_completed_fetch"] = deepcopy(state["active_fetch"])
            self.fetcher.prepare_ingest(snapshot)
        self._save(state, "after_fetch_saved")

    def _queue_upgrades(self, state, requests):
        for request in requests:
            if not isinstance(request, dict) or set(request) != {"tweet_id", "evidence", "basis"}:
                raise ValueError("Upgrade requires tweet_id, evidence and basis")
            tweet_id, evidence, basis = request["tweet_id"], request["evidence"], request["basis"]
            if not all(isinstance(v, str) and v.strip() for v in (tweet_id, evidence, basis)):
                raise ValueError("Explicit upgrade evidence and basis required")
            if len(evidence) > 100_000 or len(basis) > 1000:
                raise ValueError("Upgrade evidence too large")
            processed = state["processed_ids"].get(tweet_id)
            if not processed or processed["classification"]["label"] != "POSSIBLE_RESET":
                continue
            digest = hashlib.sha256(json.dumps([evidence, basis], ensure_ascii=False).encode()).hexdigest()
            if digest in processed["evidence_hashes"] or tweet_id in state["inbox"]:
                continue
            state["inbox"][tweet_id] = {"tweet": deepcopy(processed["tweet"]), "attempts": 0,
                "retryable": True, "next_retry_at": None, "last_error": None,
                "upgrade": {"hash": digest, "evidence": evidence, "basis": basis}}
            self._save(state)

    def _classify(self, state, result):
        entries = sorted(list(state["inbox"].items()), key=lambda pair: (parse_time(pair[1]["tweet"]["created_at"]), int(pair[0])))
        for tweet_id, entry in entries:
            if not entry.get("retryable", True) or not self._due(entry.get("next_retry_at")):
                continue
            if tweet_id in state["processed_ids"] and not entry.get("upgrade"):
                del state["inbox"][tweet_id]
                self._save(state)
                continue
            retry_deadline = self.monotonic() + self.config.classification_retry_budget_seconds
            for attempt in range(self.config.retry_attempts):
                entry["attempts"] += 1
                self._save(state)
                tweet = deepcopy(entry["tweet"])
                if entry.get("upgrade"):
                    evidence = entry["upgrade"]
                    tweet["context"] = json.dumps({"original_context": tweet.get("context"),
                        "new_evidence": evidence["evidence"], "reclassification_basis": evidence["basis"]}, ensure_ascii=False)
                try:
                    if isinstance(self.classifier, BudgetedClassifier):
                        reply = self.classifier.classify_with_deadline(tweet, now=self._now(),
                            timezone="Asia/Shanghai", deadline=retry_deadline)
                    else:
                        reply = self.classifier.classify(tweet, now=self._now(), timezone="Asia/Shanghai")
                    classification = parse_classification(reply)
                except Exception as exc:
                    retryable = not isinstance(exc, StageFailure) or exc.retryable
                    retry_after = exc.retry_after_seconds if isinstance(exc, StageFailure) else None
                    code = "CLASSIFICATION_INVALID" if isinstance(exc, ClassificationError) else "CLASSIFIER_FAILED"
                    try:
                        retry_at = timestamp(self._now() + timedelta(seconds=max(self.config.failure_cooldown_seconds, retry_after or 0)))
                    except (OverflowError, ValueError):
                        # An unrepresentable server deadline must not cause early retries.
                        retryable, retry_at = False, None
                    entry.update(last_error=code, retryable=retryable,
                                 next_retry_at=retry_at)
                    state["failed_items"][f"classify:{tweet_id}"] = {"stage": "CLASSIFY", "error_code": code,
                        "retryable": retryable, "next_retry_at": entry["next_retry_at"]}
                    self._save(state)
                    if retryable and attempt + 1 < self.config.retry_attempts:
                        delay = max(self.config.retry_delays[min(attempt, len(self.config.retry_delays) - 1)], retry_after or 0)
                        # Budgeted adapters share this deadline across requests and
                        # sleeps; third-party classifiers must honor their own timeout.
                        if self.monotonic() + delay < retry_deadline:
                            self.sleep(delay)
                            if self.monotonic() < retry_deadline:
                                continue
                    break
                self.fault_hook("after_classification_response", deepcopy(state))
                previous = state["processed_ids"].get(tweet_id)
                history = deepcopy(previous["history"]) if previous else []
                hashes = list(previous["evidence_hashes"]) if previous else []
                upgrade = entry.get("upgrade")
                if upgrade:
                    hashes.append(upgrade["hash"])
                    history.append({"classification": classification, "evidence_hash": upgrade["hash"],
                                    "basis": upgrade["basis"], "at": timestamp(self._now())})
                    # Only this explicitly evidenced transition creates an upgrade email.
                    effective = classification if classification["label"] == "PRE_RESET" else previous["classification"]
                else:
                    effective = classification
                state["processed_ids"][tweet_id] = {"tweet": entry["tweet"], "classification": effective,
                    "processed_at": timestamp(self._now()), "prompt_version": "offline-core-v1",
                    "evidence_hashes": hashes, "history": history}
                if not upgrade or classification["label"] == "PRE_RESET":
                    notification = build_notification(entry["tweet"], effective, self.config.email_to, now=self._now())
                    if notification:
                        self._enqueue(state, notification)
                del state["inbox"][tweet_id]
                state["failed_items"].pop(f"classify:{tweet_id}", None)
                self._save(state, "after_classification_saved")
                result.classified += 1
                break

    def _enqueue(self, state, notification):
        key = notification["key"]
        if key in state["pending_notifications"] or key in state["notified_ids"]:
            return
        if self.test_notifications:
            notification = dict(notification, subject="【ResetMonitor 测试】" + notification["subject"])
        state["pending_notifications"][key] = dict(notification, status="PENDING", attempts=0, failure_count=0,
            retryable=True, next_retry_at=None, sent_at=None, last_error=None, unknown_retry_used=0,
            uncertain_since=None, needs_attention=False, had_unknown=False)

    def _due(self, value):
        return value is None or parse_time(value) <= self._now()

    def _mark_unknown(self, notification):
        notification["status"] = "UNKNOWN"
        notification["had_unknown"] = True
        notification["last_error"] = "SMTP_UNCERTAIN"
        if notification["uncertain_since"] is None:
            notification["uncertain_since"] = timestamp(self._now())
        if notification["unknown_retry_used"]:
            notification.update(needs_attention=True, next_retry_at=None)
        else:
            notification["next_retry_at"] = timestamp(parse_time(notification["uncertain_since"]) + timedelta(seconds=self.config.unknown_cooldown_seconds))

    def _recover_sending(self, state):
        changed = False
        for notification in state["pending_notifications"].values():
            if notification["status"] == "SENDING":
                self._mark_unknown(notification)
                changed = True
        if changed:
            self._save(state)

    def _deliver(self, state, result, only_key=None):
        for key, notification in list(state["pending_notifications"].items()):
            if only_key is not None and key != only_key:
                continue
            if notification["status"] == "SENT" or notification["needs_attention"]:
                continue
            if not notification["retryable"] or not self._due(notification["next_retry_at"]):
                continue
            if notification["had_unknown"]:
                if notification["unknown_retry_used"]:
                    notification["needs_attention"] = True
                    self._save(state)
                    continue
                # Consume the only uncertainty resend budget before SENDING.
                notification["unknown_retry_used"] = 1
                notification["needs_attention"] = True
                self._save(state)
            notification.update(status="SENDING", needs_attention=False, attempts=notification["attempts"] + 1)
            self._save(state, "after_sending_saved")
            try:
                delivered = self.notifier.send(deepcopy(notification))
                if not isinstance(delivered, DeliveryResult):
                    raise ValueError("Invalid transport result")
            except Exception:
                delivered = DeliveryResult("UNKNOWN")
            self.fault_hook("after_delivery", deepcopy(state))
            if delivered.status == "SENT":
                notification.update(status="SENT", sent_at=timestamp(self._now()), next_retry_at=None, last_error=None)
                state["notified_ids"][key] = {"sent_at": notification["sent_at"], "message_id": notification["message_id"]}
                state["failed_items"].pop(f"notify:{key}", None)
                result.sent += 1
            elif delivered.status == "UNKNOWN":
                self._mark_unknown(notification)
            else:
                notification["failure_count"] += 1
                delay = min(86400, self.config.failure_cooldown_seconds * 2 ** min(notification["failure_count"] - 1, 4))
                notification.update(status="FAILED", last_error="SMTP_CONFIRMED_FAILURE", retryable=delivered.retryable,
                    next_retry_at=timestamp(self._now() + timedelta(seconds=delay)))
                if notification["had_unknown"] or not delivered.retryable:
                    notification.update(needs_attention=True, next_retry_at=None)
            if notification["status"] != "SENT":
                state["failed_items"][f"notify:{key}"] = {"stage": "NOTIFY", "error_code": notification["last_error"],
                    "retryable": notification["retryable"] and not notification["needs_attention"], "next_retry_at": notification["next_retry_at"]}
            # SENT and notified_ids, or the failure outcome, are one commit.
            self._save(state, "after_delivery_saved")

    def _health(self, state, error):
        health = state["health"]
        incident_key = None
        if error:
            health["consecutive_failures"] += 1
            health["last_error"] = error
            if health["consecutive_failures"] >= 3 and not health["incident_id"]:
                health["incident_id"] = uuid4().hex
                notice = build_failure_notification(health["incident_id"], self.config.email_to,
                    now=self._now(), last_success_at=state["last_success_at"],
                    failures=health["consecutive_failures"], error_code=error)
                self._enqueue(state, notice)
                incident_key = notice["key"]
        else:
            if health["incident_id"]:
                health["recovered_at"] = timestamp(self._now())
            health.update(consecutive_failures=0, last_error=None, incident_id=None)
            state["last_success_at"] = timestamp(self._now())
        self._save(state)
        return incident_key
