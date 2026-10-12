"""Project credentials held by Windows Credential Manager, never by local files.

The fixed generic target belongs to this project's Task Map binding. Persistence
is limited to the current Windows user on this computer; no roaming, registry,
environment, .env, or plaintext fallback is used. Importing this module performs
no credential operations. Python objects still hold secrets in process memory.

Win32 definitions were checked against Microsoft's CREDENTIALW, CredReadW,
CredWriteW, CredDeleteW, and CredFree documentation on 2026-10-11.
"""
import ctypes
import json
import os


from monitor.local_settings import SENDER as LOCAL_SENDER, PROJECT_BINDING as LOCAL_BINDING
from monitor.credentials import CredentialInputError, ProjectCredentials


PROJECT_BINDING = LOCAL_BINDING
TARGET_NAME = "CodexResetMonitor:" + PROJECT_BINDING + ":credentials-v1"
SMTP_USER = LOCAL_SENDER
MAX_BLOB_BYTES = 5 * 512
_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_ERROR_NOT_FOUND = 1168
_FIELDS = frozenset(("schema_version", "project_binding", "smtp_user",
                     "apify_token", "deepseek_api_key", "smtp_password"))
_ERROR_CODES = frozenset((
    "CREDENTIAL_STORE_UNAVAILABLE", "CREDENTIAL_STORE_READ_FAILED",
    "CREDENTIAL_STORE_WRITE_FAILED", "CREDENTIAL_STORE_DELETE_FAILED",
    "CREDENTIAL_STORE_INVALID", "CREDENTIAL_STORE_TOO_LARGE",
))


class CredentialStoreError(ValueError):
    """A fixed diagnostic code with no provider or secret-bearing details."""
    def __init__(self, code="CREDENTIAL_STORE_INVALID"):
        self.code = code if code in _ERROR_CODES else "CREDENTIAL_STORE_INVALID"
        super().__init__(self.code)


# Fixed-width Win32 integers also make fake tests independent of the test OS.
_DWORD = ctypes.c_uint32
_BYTE = ctypes.c_ubyte


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", _DWORD), ("dwHighDateTime", _DWORD)]


class _CREDENTIAL_ATTRIBUTEW(ctypes.Structure):
    _fields_ = [("Keyword", ctypes.c_wchar_p), ("Flags", _DWORD),
                ("ValueSize", _DWORD), ("Value", ctypes.POINTER(_BYTE))]


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", _DWORD), ("Type", _DWORD),
        ("TargetName", ctypes.c_wchar_p), ("Comment", ctypes.c_wchar_p),
        ("LastWritten", _FILETIME), ("CredentialBlobSize", _DWORD),
        ("CredentialBlob", ctypes.POINTER(_BYTE)), ("Persist", _DWORD),
        ("AttributeCount", _DWORD),
        ("Attributes", ctypes.POINTER(_CREDENTIAL_ATTRIBUTEW)),
        ("TargetAlias", ctypes.c_wchar_p), ("UserName", ctypes.c_wchar_p),
    ]


_PCREDENTIALW = ctypes.POINTER(_CREDENTIALW)


class _WindowsCredentialBackend:
    """Small Win32 wrapper; injectable API is used only for synthetic tests.

    A different target_name permits an isolated synthetic system round trip.
    Public project operations always use TARGET_NAME unless a test backend is
    explicitly supplied. No enumeration or access to unrelated targets exists.
    """
    __slots__ = ("_target", "_api", "_get_error")

    def __init__(self, *, target_name=TARGET_NAME, api=None, get_error=None):
        if (not isinstance(target_name, str) or not 0 < len(target_name) <= 32767
                or "\x00" in target_name):
            raise CredentialStoreError()
        self._target = target_name
        try:
            if api is None:
                if os.name != "nt":
                    raise CredentialStoreError("CREDENTIAL_STORE_UNAVAILABLE")
                api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
                get_error = ctypes.get_last_error
            if not callable(get_error):
                raise CredentialStoreError("CREDENTIAL_STORE_UNAVAILABLE")
            api.CredWriteW.argtypes = [_PCREDENTIALW, _DWORD]
            api.CredWriteW.restype = ctypes.c_int32
            api.CredReadW.argtypes = [ctypes.c_wchar_p, _DWORD, _DWORD,
                                     ctypes.POINTER(_PCREDENTIALW)]
            api.CredReadW.restype = ctypes.c_int32
            api.CredDeleteW.argtypes = [ctypes.c_wchar_p, _DWORD, _DWORD]
            api.CredDeleteW.restype = ctypes.c_int32
            api.CredFree.argtypes = [ctypes.c_void_p]
            api.CredFree.restype = None
            self._api, self._get_error = api, get_error
        except CredentialStoreError:
            raise
        except Exception:
            raise CredentialStoreError("CREDENTIAL_STORE_UNAVAILABLE") from None

    def __repr__(self):
        return "_WindowsCredentialBackend(<redacted>)"

    def _read(self, *, include_blob):
        pointer = _PCREDENTIALW()
        try:
            if not self._api.CredReadW(self._target, _CRED_TYPE_GENERIC, 0,
                                       ctypes.byref(pointer)):
                # GetLastError must be captured before another Win32 call.
                error = self._get_error()
                if error == _ERROR_NOT_FOUND:
                    return None
                raise CredentialStoreError("CREDENTIAL_STORE_READ_FAILED")
            if not pointer:
                raise CredentialStoreError("CREDENTIAL_STORE_INVALID")
            credential = pointer.contents
            if (credential.Type != _CRED_TYPE_GENERIC
                    or credential.TargetName != self._target
                    or credential.UserName != SMTP_USER
                    or credential.Persist != _CRED_PERSIST_LOCAL_MACHINE
                    or not 0 < credential.CredentialBlobSize <= MAX_BLOB_BYTES
                    or not credential.CredentialBlob):
                raise CredentialStoreError("CREDENTIAL_STORE_INVALID")
            if not include_blob:
                return True
            # Copy before CredFree releases the single allocation and pointers.
            return ctypes.string_at(credential.CredentialBlob,
                                    credential.CredentialBlobSize)
        except CredentialStoreError:
            raise
        except Exception:
            raise CredentialStoreError("CREDENTIAL_STORE_READ_FAILED") from None
        finally:
            if pointer:
                try:
                    self._api.CredFree(pointer)
                except Exception:
                    raise CredentialStoreError("CREDENTIAL_STORE_READ_FAILED") from None

    def read(self):
        return self._read(include_blob=True)

    def exists(self):
        return self._read(include_blob=False) is not None

    def write(self, blob):
        if not isinstance(blob, bytes) or not 0 < len(blob) <= MAX_BLOB_BYTES:
            raise CredentialStoreError("CREDENTIAL_STORE_TOO_LARGE")
        buffer = (_BYTE * len(blob)).from_buffer_copy(blob)
        credential = _CREDENTIALW()
        credential.Type = _CRED_TYPE_GENERIC
        credential.TargetName = self._target
        credential.Comment = "ResetMonitor project credentials v1"
        credential.CredentialBlobSize = len(blob)
        credential.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(_BYTE))
        credential.Persist = _CRED_PERSIST_LOCAL_MACHINE
        credential.UserName = SMTP_USER
        try:
            if not self._api.CredWriteW(ctypes.byref(credential), 0):
                raise CredentialStoreError("CREDENTIAL_STORE_WRITE_FAILED")
        except CredentialStoreError:
            raise
        except Exception:
            raise CredentialStoreError("CREDENTIAL_STORE_WRITE_FAILED") from None
        finally:
            # Clear the mutable native staging buffer; Python strings/bytes have
            # no reliable erase operation and are not claimed to be zeroized.
            ctypes.memset(ctypes.addressof(buffer), 0, len(buffer))

    def delete(self):
        try:
            if self._api.CredDeleteW(self._target, _CRED_TYPE_GENERIC, 0):
                return True
            error = self._get_error()
            if error == _ERROR_NOT_FOUND:
                return False
            raise CredentialStoreError("CREDENTIAL_STORE_DELETE_FAILED")
        except CredentialStoreError:
            raise
        except Exception:
            raise CredentialStoreError("CREDENTIAL_STORE_DELETE_FAILED") from None


def _no_duplicate_keys(pairs):
    values = {}
    for key, value in pairs:
        if key in values:
            raise CredentialStoreError()
        values[key] = value
    return values


def _decode(blob):
    try:
        if not isinstance(blob, bytes) or not 0 < len(blob) <= MAX_BLOB_BYTES:
            raise CredentialStoreError()
        values = json.loads(blob.decode("utf-8", errors="strict"),
                            object_pairs_hook=_no_duplicate_keys)
        if (not isinstance(values, dict) or set(values) != _FIELDS
                or type(values["schema_version"]) is not int
                or values["schema_version"] != 1
                or values["project_binding"] != PROJECT_BINDING
                or values["smtp_user"] != SMTP_USER
                or not isinstance(values["smtp_password"], str)
                or len(values["smtp_password"]) != 16):
            raise CredentialStoreError()
        return ProjectCredentials(
            apify_token=values["apify_token"],
            deepseek_api_key=values["deepseek_api_key"],
            smtp_user=values["smtp_user"], smtp_password=values["smtp_password"])
    except (CredentialInputError, ValueError, TypeError, UnicodeError):
        raise CredentialStoreError() from None


def load_credentials(*, backend=None):
    """Load only this project's credentials; return None when absent (1168)."""
    try:
        selected = _WindowsCredentialBackend() if backend is None else backend
        blob = selected.read()
        return None if blob is None else _decode(blob)
    except CredentialStoreError:
        raise
    except Exception:
        raise CredentialStoreError("CREDENTIAL_STORE_READ_FAILED") from None


def has_credentials(*, backend=None):
    """Report presence only; the Win32 backend does not copy secret data."""
    try:
        selected = _WindowsCredentialBackend() if backend is None else backend
        result = selected.exists()
        if type(result) is not bool:
            raise CredentialStoreError()
        return result
    except CredentialStoreError:
        raise
    except Exception:
        raise CredentialStoreError("CREDENTIAL_STORE_READ_FAILED") from None


def save_credentials(credentials, *, backend=None):
    """Create or replace the single project credential, without returning it."""
    try:
        if (not isinstance(credentials, ProjectCredentials)
                or credentials.smtp_user != SMTP_USER):
            raise CredentialStoreError()
        values = {"schema_version": 1, "project_binding": PROJECT_BINDING,
                  "smtp_user": SMTP_USER, "apify_token": credentials.apify_token,
                  "deepseek_api_key": credentials.deepseek_api_key,
                  "smtp_password": credentials.smtp_password}
        blob = json.dumps(values, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        if len(blob) > MAX_BLOB_BYTES:
            raise CredentialStoreError("CREDENTIAL_STORE_TOO_LARGE")
        # Revalidate fields before any OS write, including unusual subclasses.
        _decode(blob)
        selected = _WindowsCredentialBackend() if backend is None else backend
        selected.write(blob)
    except CredentialStoreError:
        raise
    except Exception:
        raise CredentialStoreError("CREDENTIAL_STORE_WRITE_FAILED") from None


def delete_credentials(*, backend=None):
    """Delete only this project's record; return False when it was absent."""
    try:
        selected = _WindowsCredentialBackend() if backend is None else backend
        result = selected.delete()
        if type(result) is not bool:
            raise CredentialStoreError()
        return result
    except CredentialStoreError:
        raise
    except Exception:
        raise CredentialStoreError("CREDENTIAL_STORE_DELETE_FAILED") from None
