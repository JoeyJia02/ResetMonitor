"""Explicit project credential inputs, with optional Windows credential storage.

These functions are code for a future authorized human invocation. Tests pass
synthetic mappings/callables. Nothing reads the environment at import time.
"""
import getpass
import os
import sys
import warnings


class CredentialInputError(ValueError):
    def __init__(self):
        super().__init__("PROJECT_CREDENTIAL_INPUT_REQUIRED")


class ProjectCredentials:
    """Not a dataclass and not JSON serializable; representations contain no values."""
    __slots__ = ("_apify_token", "_deepseek_api_key", "_smtp_user", "_smtp_password")

    def __init__(self, *, apify_token, deepseek_api_key, smtp_user, smtp_password):
        values = (apify_token, deepseek_api_key, smtp_user, smtp_password)
        if any(not isinstance(value, str) or not 0 < len(value) <= 4096
               or any(ord(c) < 33 or ord(c) > 126 for c in value) for value in values):
            raise CredentialInputError()
        self._apify_token, self._deepseek_api_key, self._smtp_user, self._smtp_password = values

    @property
    def apify_token(self): return self._apify_token

    @property
    def deepseek_api_key(self): return self._deepseek_api_key

    @property
    def smtp_user(self): return self._smtp_user

    @property
    def smtp_password(self): return self._smtp_password

    def __repr__(self):
        return "ProjectCredentials(<redacted>)"


_INPUTS = (
    ("apify_token", "RESET_MONITOR_APIFY_TOKEN", "ResetMonitor Apify token: "),
    ("deepseek_api_key", "RESET_MONITOR_DEEPSEEK_API_KEY", "ResetMonitor DeepSeek API key: "),
    ("smtp_user", "RESET_MONITOR_SMTP_USER", "ResetMonitor SMTP user: "),
    ("smtp_password", "RESET_MONITOR_SMTP_PASSWORD", "ResetMonitor SMTP password: "),
)


def read_project_credentials(source, *, environment=None, hidden_input=None):
    """Read only four project-specific values after the public preflight passes.

    Environment mode does not inspect global API keys, .env files or other apps.
    Prompt mode refuses getpass's echoing fallback. Stored mode reads only the
    project's credential entry and never prompts or falls back to environment
    values. This function does not save credentials. Python strings cannot
    promise deterministic erasure from process memory.
    """
    try:
        if source == "stored":
            # Import and access the Windows store only for explicit stored input,
            # after the caller has completed its public preflight.
            from .credential_store import load_credentials
            credentials = load_credentials()
            if not isinstance(credentials, ProjectCredentials):
                raise CredentialInputError()
            return credentials
        if source == "env":
            mapping = os.environ if environment is None else environment
            values = {name: mapping.get(variable) for name, variable, _ in _INPUTS}
        elif source == "prompt":
            if hidden_input is None:
                if not sys.stdin.isatty() or not sys.stderr.isatty():
                    raise CredentialInputError()
                hidden_input = getpass.getpass
            if not callable(hidden_input):
                raise CredentialInputError()
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                values = {name: hidden_input(label) for name, _, label in _INPUTS}
        else:
            raise CredentialInputError()
        return ProjectCredentials(**values)
    except (Exception, KeyboardInterrupt):
        raise CredentialInputError() from None
