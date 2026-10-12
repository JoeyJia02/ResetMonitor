"""Small, read-only status projection for the desktop window."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path


BEIJING = timezone(timedelta(hours=8))


def read_background_status(*, path=None, now=None):
    missing = {"running": False, "title": "未检测到后台", "detail": "后台需要当前电脑保持开机、联网及登录。",
               "last_run": "暂无运行记录", "next_run": "08:00 / 13:00 / 20:00"}
    now = datetime.now(timezone.utc) if now is None else now
    if path is None:
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            return missing
        path = Path(base) / "CodexResetMonitor" / "background" / "status.json"
    try:
        path = Path(path)
        if not path.is_file() or path.stat().st_size > 16384:
            return missing
        value = json.loads(path.read_text(encoding="utf-8"))
        heartbeat = datetime.fromisoformat(value["heartbeat_at"])
        age = (now - heartbeat).total_seconds()
        running = value.get("state") == "RUNNING" and 0 <= age <= 120
        local = now.astimezone(BEIJING)
        upcoming = [local.replace(hour=hour, minute=0, second=0, microsecond=0) for hour in (8, 13, 20)]
        next_run = next((slot for slot in upcoming if slot > local), upcoming[0] + timedelta(days=1))
        slot = value.get("last_slot") or {}
        labels = {"SENT": "邮件已提交", "NO_RELEVANT_ITEMS": "暂无相关公告", "NO_NEW_ITEMS": "暂无新推文",
                  "UNKNOWN": "上轮未完成", "FAILED": "上轮未完成", "EMPTY": "本轮无数据"}
        last = labels.get(slot.get("status"), "暂无运行记录")
        return {"running": running, "title": "后台运行中" if running else "后台心跳已过期",
                "detail": "最近心跳 · " + heartbeat.astimezone(BEIJING).strftime("%H:%M:%S"),
                "last_run": last, "next_run": ("明天 " if next_run.date() != local.date() else "今天 ") + next_run.strftime("%H:%M")}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return missing
