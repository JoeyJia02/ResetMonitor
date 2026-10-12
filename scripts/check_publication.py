"""Audit staged blobs, never working-tree replacements or credential contents.

Reports rule names and locations only. This is an aid, not a complete secret
scanner, and does not inspect existing commits or remote history.
"""
from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_DIRECTORIES = {"runtime", "evidence", "logs", "artifacts", ".claude", ".codex", ".agents"}
PRIVATE_NAMES = {"config.local.json", "AGENTS.md", "PROJECT_STATUS.md", "TASKS.md", "DECISIONS.md",
                 "TECHNICAL_PLAN.md", "TEST_RESULTS.md", "state.json", "keys.txt", "credentials.json"}
PRIVATE_SCRIPTS = {"scripts/resume_failed_full_chain.py", "tests/test_full_chain_recovery.py"}
EMAIL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]*[A-Za-z0-9])?@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")
SECRET = re.compile(r"\b(?:apify_api_[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9_-]{20,})\b")
USER_PATH = re.compile(r"(?i)[A-Z]:[\\/]+Users[\\/]+([^\\/\s\"'<>]+)")


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str


def private_path(name):
    path = PurePosixPath(name)
    filename = path.name
    lower = filename.lower()
    return (any(part.lower() in PRIVATE_DIRECTORIES for part in path.parts)
            or lower in {item.lower() for item in PRIVATE_NAMES}
            or name in PRIVATE_SCRIPTS
            or lower.startswith("config.local.json.")
            or lower.startswith(("source_fingerprints", "background_", "repair_"))
            and path.suffix.lower() in {".md", ".json"}
            or (lower.startswith(".env") and lower != ".env.example")
            or path.suffix.lower() in {".log", ".lnk", ".pem", ".key", ".pfx", ".p12", ".lock", ".pyc"}
            or ("cookies" in lower and path.suffix.lower() == ".json"))


def example_domain(domain):
    domain = domain.lower().rstrip(".")
    return (domain in {"example.com", "example.net", "example.org"}
            or domain.endswith((".invalid", ".test", ".example", ".example.com", ".example.net", ".example.org")))


def scan_blob(name, data, local_values=()):
    if private_path(name):
        return [Finding(name, 0, "PRIVATE_FILE")]
    try:
        content = data.decode("utf-8-sig")
    except UnicodeError:
        return [Finding(name, 0, "BINARY_REQUIRES_REVIEW")]
    findings = []
    for number, line in enumerate(content.splitlines(), 1):
        rules = set()
        if SECRET.search(line):
            rules.add("POSSIBLE_API_SECRET")
        if any(not example_domain(match.group(1)) for match in EMAIL.finditer(line)):
            rules.add("NON_EXAMPLE_EMAIL")
        if any(match.group(1).lower() not in {"public", "example", "user", "username"}
               for match in USER_PATH.finditer(line)):
            rules.add("PERSONAL_WINDOWS_PATH")
        if any(value.casefold() in line.casefold() for value in local_values if value):
            rules.add("LOCAL_PRIVATE_VALUE")
        findings.extend(Finding(name, number, rule) for rule in sorted(rules))
    return findings


def git(*arguments, root=ROOT):
    result = subprocess.run(["git", "-C", str(root), *arguments], capture_output=True, check=True)
    return result.stdout


def staged_blobs(root=ROOT):
    # Read by object ID from the index. Unstaged redaction cannot hide a secret.
    for entry in git("ls-files", "--stage", "-z", root=root).split(b"\0"):
        if not entry:
            continue
        metadata, name = entry.split(b"\t", 1)
        mode, oid, stage = metadata.decode("ascii").split()
        name = name.decode("utf-8")
        if stage != "0" or mode not in {"100644", "100755"}:
            yield name, None
        else:
            yield name, git("cat-file", "blob", oid, root=root)


def local_private_values(root=ROOT):
    path = root / "config.local.json"
    if not path.exists():
        return ()
    if path.stat().st_size > 4096:
        raise ValueError()
    config = json.loads(path.read_text(encoding="utf-8"))
    result = []
    for key in ("sender", "recipient", "project_binding"):
        value = config.get(key)
        if not isinstance(value, str):
            raise ValueError()
        if key == "project_binding":
            if value != "resetmonitor-default":
                result.append(value)
        elif "@" in value and not example_domain(value.rsplit("@", 1)[1]):
            result.append(value)
    return tuple(result)


def main():
    try:
        local_values = local_private_values()
        count = 0
        findings = []
        for name, data in staged_blobs():
            count += 1
            if data is None:
                findings.append(Finding(name, 0, "INDEX_ENTRY_REQUIRES_REVIEW"))
            else:
                findings.extend(scan_blob(name, data, local_values))
        if not count:
            print("PUBLICATION_CHECK_NO_STAGED_FILES")
            return 2
        for finding in findings:
            print(f"{finding.path}:{finding.line}: {finding.rule}")
        print(f"PUBLICATION_CHECK_{'FAILED' if findings else 'OK'} files={count} findings={len(findings)}")
        return 1 if findings else 0
    except (OSError, ValueError, subprocess.SubprocessError):
        print("PUBLICATION_CHECK_UNAVAILABLE")
        return 2


if __name__ == "__main__":
    sys.exit(main())
