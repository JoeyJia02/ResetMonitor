"""Non-secret, machine-local routing. API credentials stay in Windows Vault.

Only this small JSON file contains personal addresses and the local vault
namespace. It is excluded from Git. Imports never read credential contents.
"""
import json
from pathlib import Path
import re


CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.local.json"
DEFAULTS = {"schema_version": 1, "sender": "sender@example.invalid",
            "recipient": "recipient@example.invalid", "project_binding": "resetmonitor-default"}


class LocalSettingsError(ValueError):
    def __init__(self):
        super().__init__("LOCAL_SETTINGS_INVALID")


def _validate(value):
    if (not isinstance(value, dict) or set(value) != set(DEFAULTS)
            or type(value["schema_version"]) is not int or value["schema_version"] != 1):
        raise LocalSettingsError()
    for field in ("sender", "recipient"):
        address = value[field]
        if (not isinstance(address, str) or len(address) > 254 or not address.isascii()
                or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", address)):
            raise LocalSettingsError()
    binding = value["project_binding"]
    if not isinstance(binding, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,99}", binding):
        raise LocalSettingsError()
    return dict(value)


def read_settings(path=None):
    path = CONFIG_PATH if path is None else Path(path)
    try:
        if not path.exists():
            return dict(DEFAULTS)
        if path.stat().st_size > 4096:
            raise LocalSettingsError()
        return _validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, UnicodeError):
        raise LocalSettingsError() from None


def save_routing(sender, recipient, *, path=None):
    path = CONFIG_PATH if path is None else Path(path)
    current = read_settings(path)
    current.update(sender=sender, recipient=recipient)
    value = _validate(current)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    except OSError:
        raise LocalSettingsError() from None
    return value


def mask_address(address):
    if not isinstance(address, str) or "@" not in address:
        return "未配置"
    local, domain = address.rsplit("@", 1)
    if domain == "example.invalid":
        return "未配置"
    return local[:2] + "••••" + "@" + domain


_settings = read_settings()
SENDER = _settings["sender"]
RECIPIENT = _settings["recipient"]
PROJECT_BINDING = _settings["project_binding"]
