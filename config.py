"""Validated public configuration; normal CLI execution defaults to offline."""

from monitor.local_settings import RECIPIENT as LOCAL_RECIPIENT

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Mapping

SCHEDULE_TIMEZONE = 'Asia/Shanghai'
SCHEDULE_TIMES = ('08:00', '13:00', '20:00')


class ConfigError(ValueError):
    """Configuration is invalid; messages deliberately exclude supplied values."""


@dataclass(frozen=True)
class Config:
    state_path: Path
    target_username: str = "thsottiaux"
    schedule_timezone: str = SCHEDULE_TIMEZONE
    schedule_times: tuple[str, ...] = SCHEDULE_TIMES
    fetch_limit: int = 3
    email_to: str = LOCAL_RECIPIENT
    include_replies: bool = True
    exclude_retweets: bool = True
    target_user_id: str | None = None
    execution_mode: str = "OFFLINE"
    retry_attempts: int = 3
    retry_delays: tuple[int, ...] = (5, 15)
    failure_cooldown_seconds: int = 7200
    unknown_cooldown_seconds: int = 86400
    classification_retry_budget_seconds: int = 60

    def __post_init__(self):
        if self.execution_mode not in {"OFFLINE", "LIVE"}:
            raise ConfigError("Invalid execution mode")
        if not isinstance(self.target_username, str) or not re.fullmatch(r"@?[A-Za-z0-9_]{1,15}", self.target_username):
            raise ConfigError("TARGET_USERNAME must be a valid public username")
        object.__setattr__(self, "target_username", self.target_username.lstrip("@").lower())
        if self.include_replies is not True or self.exclude_retweets is not True:
            raise ConfigError("Authorized scope includes replies and excludes retweets")
        if self.target_user_id is not None and (not isinstance(self.target_user_id, str)
                or not re.fullmatch(r"[1-9][0-9]{0,19}", self.target_user_id)
                or int(self.target_user_id) > (1 << 64) - 1):
            raise ConfigError("TARGET_USER_ID must be a uint64 identifier or empty")

    @classmethod
    def from_env(cls, env: Mapping[str, str], project_root: Path) -> "Config":
        def value(name: str, default: str) -> str:
            supplied = env.get(name, default)
            if not isinstance(supplied, str):
                raise ConfigError(f"{name} must be text")
            return supplied.strip()

        def integer(name: str, default: str) -> int:
            try:
                return int(value(name, default))
            except (ValueError, TypeError):
                raise ConfigError(f"{name} must be an integer") from None

        if "CHECK_INTERVAL_HOURS" in env:
            raise ConfigError("CHECK_INTERVAL_HOURS was replaced by the fixed Shanghai schedule")
        schedule_timezone = value("SCHEDULE_TIMEZONE", SCHEDULE_TIMEZONE)
        schedule_times = tuple(item.strip() for item in value("SCHEDULE_TIMES", ",".join(SCHEDULE_TIMES)).split(","))
        if schedule_timezone != SCHEDULE_TIMEZONE or schedule_times != SCHEDULE_TIMES:
            raise ConfigError("Schedule must use Asia/Shanghai at 08:00, 13:00, 20:00")
        limit = integer("FETCH_LIMIT", "3")
        if limit != 3:
            raise ConfigError("FETCH_LIMIT must be 3")

        username = value("TARGET_USERNAME", "thsottiaux").lower()
        if username.startswith("@"):
            username = username[1:]
        if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", username):
            raise ConfigError("TARGET_USERNAME must be a valid public username")

        # Validate the unstripped value so unsafe controls cannot disappear at edges.
        raw_recipient = env.get("EMAIL_TO", LOCAL_RECIPIENT)
        if not isinstance(raw_recipient, str) or any(ord(c) < 32 or ord(c) == 127 for c in raw_recipient):
            raise ConfigError("EMAIL_TO must contain one plain email address")
        recipient = raw_recipient.strip()
        if not re.fullmatch(r"[^\s@,;<>:\x00-\x1f\x7f]+@[^\s@,;<>:\x00-\x1f\x7f]+\.[^\s@,;<>:\x00-\x1f\x7f]+", recipient):
            raise ConfigError("EMAIL_TO must contain one plain email address")

        if value("INCLUDE_REPLIES", "true").lower() != "true" or value("EXCLUDE_RETWEETS", "true").lower() != "true":
            raise ConfigError("Authorized scope requires INCLUDE_REPLIES=true and EXCLUDE_RETWEETS=true")
        target_user_id = value("TARGET_USER_ID", "") or None

        filename = value("STATE_FILE", "state.json")
        if not filename or "\x00" in filename:
            raise ConfigError("STATE_FILE must name a state file")
        try:
            root = Path(project_root).resolve()
            path = Path(filename)
            state_path = (path if path.is_absolute() else root / path).resolve()
        except (OSError, ValueError, TypeError):
            raise ConfigError("STATE_FILE must name a valid state file") from None
        if state_path == root or state_path.is_dir():
            raise ConfigError("STATE_FILE must name a file rather than a directory")
        return cls(
            state_path=state_path,
            target_username=username,
            schedule_timezone=schedule_timezone,
            schedule_times=schedule_times,
            fetch_limit=limit,
            email_to=recipient,
            include_replies=True,
            exclude_retweets=True,
            target_user_id=target_user_id,
        )
