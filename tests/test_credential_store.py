"""Synthetic Credential Manager tests; never access the user's actual vault."""
import ctypes
import json
import unittest
from unittest import mock

from monitor.credentials import ProjectCredentials
from monitor import credential_store as store


def credentials(**changes):
    values = {"apify_token": "apify-SYNTHETIC-NOT-A-REAL-TOKEN",
              "deepseek_api_key": "SYNTHETIC-NOT-A-REAL-KEY",
              "smtp_user": store.SMTP_USER,
              "smtp_password": "SYNTHETIC_PASS16"}
    values.update(changes)
    return ProjectCredentials(**values)


class _Function:
    def __init__(self, operation):
        self.operation = operation

    def __call__(self, *args):
        return self.operation(*args)


class FakeWinAPI:
    """Imitates allocations and LastError; all payloads are synthetic."""
    def __init__(self):
        self.saved = None
        self.error = 0
        self.read_error = None
        self.write_error = None
        self.delete_error = None
        self.override = {}
        self.calls = []
        self.allocations = []
        self.freed = 0
        self.CredReadW = _Function(self.read)
        self.CredWriteW = _Function(self.write)
        self.CredDeleteW = _Function(self.delete)
        self.CredFree = _Function(self.free)

    def write(self, pointer, flags):
        value = ctypes.cast(pointer, store._PCREDENTIALW).contents
        self.calls.append(("write", value.TargetName, value.Type, flags))
        if self.write_error:
            self.error = self.write_error
            return 0
        self.saved = {"target": value.TargetName, "type": value.Type,
                      "persist": value.Persist, "username": value.UserName,
                      "flags": value.Flags, "attributes": value.AttributeCount,
                      "alias": value.TargetAlias,
                      "blob": ctypes.string_at(value.CredentialBlob,
                                               value.CredentialBlobSize)}
        return 1

    def read(self, target, kind, flags, output):
        self.calls.append(("read", target, kind, flags))
        if self.read_error or self.saved is None:
            self.error = self.read_error or 1168
            return 0
        saved = self.saved
        blob = (store._BYTE * len(saved["blob"])).from_buffer_copy(saved["blob"])
        value = store._CREDENTIALW()
        value.Type = saved["type"]
        value.TargetName = saved["target"]
        value.Persist = saved["persist"]
        value.UserName = saved["username"]
        value.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(store._BYTE))
        value.CredentialBlobSize = len(blob)
        for name, entry in self.override.items():
            setattr(value, name, entry)
        pointer = ctypes.pointer(value)
        self.allocations.append((value, blob, pointer))
        ctypes.cast(output, ctypes.POINTER(store._PCREDENTIALW))[0] = pointer
        return 1

    def delete(self, target, kind, flags):
        self.calls.append(("delete", target, kind, flags))
        if self.delete_error or self.saved is None:
            self.error = self.delete_error or 1168
            return 0
        self.saved = None
        return 1

    def free(self, pointer):
        self.freed += 1
        # Simulate the lifetime ending: a caller must have copied first.
        value = ctypes.cast(pointer, store._PCREDENTIALW).contents
        if value.CredentialBlob and value.CredentialBlobSize <= store.MAX_BLOB_BYTES:
            ctypes.memset(value.CredentialBlob, 0, value.CredentialBlobSize)


class MemoryBackend:
    def __init__(self, blob=None):
        self.blob = blob

    def read(self):
        return self.blob

    def write(self, blob):
        self.blob = blob

    def exists(self):
        return self.blob is not None

    def delete(self):
        present = self.blob is not None
        self.blob = None
        return present


class CredentialStoreTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeWinAPI()
        self.backend = store._WindowsCredentialBackend(
            api=self.api, get_error=lambda: self.api.error)

    def assert_code(self, code, operation):
        with self.assertRaises(store.CredentialStoreError) as raised:
            operation()
        self.assertEqual(raised.exception.code, code)
        self.assertEqual(str(raised.exception), code)
        self.assertNotIn("SYNTHETIC", repr(raised.exception))

    def save(self, value=None):
        store.save_credentials(value or credentials(), backend=self.backend)

    def test_missing_read_and_delete_are_normal_and_no_allocation(self):
        self.assertIsNone(store.load_credentials(backend=self.backend))
        self.assertFalse(store.has_credentials(backend=self.backend))
        self.assertFalse(store.delete_credentials(backend=self.backend))
        self.assertEqual(self.api.freed, 0)
        self.assertEqual(self.api.calls, [
            ("read", store.TARGET_NAME, 1, 0),
            ("read", store.TARGET_NAME, 1, 0),
            ("delete", store.TARGET_NAME, 1, 0)])

    def test_save_load_replace_delete_lifecycle(self):
        original = credentials()
        self.assertIsNone(store.save_credentials(original, backend=self.backend))
        result = store.load_credentials(backend=self.backend)
        self.assertEqual(result.apify_token, original.apify_token)
        self.assertEqual(result.deepseek_api_key, original.deepseek_api_key)
        self.assertEqual(result.smtp_user, store.SMTP_USER)
        self.assertEqual(result.smtp_password, original.smtp_password)
        self.assertEqual(self.api.freed, 1)
        replacement = credentials(smtp_password="SYNTHETIC_NEW_16")
        self.save(replacement)
        self.assertEqual(store.load_credentials(backend=self.backend).smtp_password,
                         replacement.smtp_password)
        self.assertTrue(store.delete_credentials(backend=self.backend))
        self.assertIsNone(store.load_credentials(backend=self.backend))

    def test_metadata_is_fixed_single_project_generic_persistent_local_user(self):
        self.save()
        saved = self.api.saved
        self.assertEqual(saved["target"], store.TARGET_NAME)
        self.assertEqual(saved["type"], 1)
        self.assertEqual(saved["persist"], 2)
        self.assertEqual(saved["username"], store.SMTP_USER)
        self.assertEqual(saved["flags"], 0)
        self.assertEqual(saved["attributes"], 0)
        self.assertIsNone(saved["alias"])
        payload = json.loads(saved["blob"].decode("utf-8"))
        self.assertEqual(payload["project_binding"], store.PROJECT_BINDING)
        self.assertEqual(payload["schema_version"], 1)
        self.assertLessEqual(len(saved["blob"]), 2560)

    def test_presence_does_not_copy_or_deserialize_secret_blob(self):
        self.save()
        with mock.patch.object(store.ctypes, "string_at", side_effect=AssertionError), \
                mock.patch.object(store.json, "loads", side_effect=AssertionError):
            self.assertTrue(store.has_credentials(backend=self.backend))
        self.assertEqual(self.api.freed, 1)

    def test_native_staging_buffer_is_cleared_after_write(self):
        buffers = []
        native_write = self.api.CredWriteW.operation

        def capture(pointer, flags):
            value = ctypes.cast(pointer, store._PCREDENTIALW).contents
            buffers.append((value.CredentialBlob, value.CredentialBlobSize))
            return native_write(pointer, flags)

        self.api.CredWriteW.operation = capture
        with mock.patch.object(store.ctypes, "memset", wraps=ctypes.memset) as clear:
            self.save()
        self.assertEqual(clear.call_count, 1)
        self.assertEqual(clear.call_args.args[1], 0)
        self.assertGreater(clear.call_args.args[2], 0)
        self.assertTrue(buffers)

    def test_read_errors_are_fixed_and_do_not_look_missing(self):
        self.api.read_error = 1312
        self.assert_code("CREDENTIAL_STORE_READ_FAILED",
                         lambda: store.load_credentials(backend=self.backend))
        self.assert_code("CREDENTIAL_STORE_READ_FAILED",
                         lambda: store.has_credentials(backend=self.backend))

    def test_write_errors_are_fixed_and_do_not_return_credentials(self):
        self.api.write_error = 87
        self.assert_code("CREDENTIAL_STORE_WRITE_FAILED", self.save)

    def test_delete_errors_are_fixed(self):
        self.api.delete_error = 1312
        self.assert_code("CREDENTIAL_STORE_DELETE_FAILED",
                         lambda: store.delete_credentials(backend=self.backend))

    def test_native_exceptions_cannot_expose_provider_values(self):
        for operation, code in (("CredReadW", "CREDENTIAL_STORE_READ_FAILED"),
                                ("CredWriteW", "CREDENTIAL_STORE_WRITE_FAILED"),
                                ("CredDeleteW", "CREDENTIAL_STORE_DELETE_FAILED")):
            with self.subTest(operation=operation):
                with mock.patch.object(self.api, operation,
                                       side_effect=RuntimeError("SYNTHETIC-SECRET")):
                    invocation = {"CredReadW": lambda: store.load_credentials(backend=self.backend),
                                  "CredWriteW": self.save,
                                  "CredDeleteW": lambda: store.delete_credentials(backend=self.backend)}
                    self.assert_code(code, invocation[operation])

    def test_wrong_native_metadata_is_rejected_and_freed(self):
        self.save()
        overrides = ({"Type": 2}, {"TargetName": "another-project"},
                     {"Persist": 3}, {"UserName": "other@example.com"},
                     {"CredentialBlobSize": 0},
                     {"CredentialBlobSize": store.MAX_BLOB_BYTES + 1},
                     {"CredentialBlob": ctypes.POINTER(store._BYTE)()})
        for override in overrides:
            with self.subTest(override=tuple(override)):
                self.api.override = override
                self.assert_code("CREDENTIAL_STORE_INVALID",
                                 lambda: store.load_credentials(backend=self.backend))
        self.assertEqual(self.api.freed, len(overrides))

    def test_invalid_utf8_invalid_json_and_duplicates_are_rejected(self):
        blobs = (b"\xff", b"{}", b"[]", b"null", b"{", b"{\"a\":1,\"a\":2}",
                 b"", b"x" * (store.MAX_BLOB_BYTES + 1))
        for blob in blobs:
            with self.subTest(size=len(blob)):
                self.assert_code("CREDENTIAL_STORE_INVALID", lambda:
                                 store.load_credentials(backend=MemoryBackend(blob)))

    def test_duplicate_valid_schema_key_rejected(self):
        memory = MemoryBackend()
        store.save_credentials(credentials(), backend=memory)
        blob = memory.blob[:-1] + b',"schema_version":1}'
        self.assert_code("CREDENTIAL_STORE_INVALID", lambda:
                         store.load_credentials(backend=MemoryBackend(blob)))

    def test_json_requires_exact_fields_binding_version_and_sender(self):
        memory = MemoryBackend()
        store.save_credentials(credentials(), backend=memory)
        valid = json.loads(memory.blob)
        for field, value in (("schema_version", True), ("schema_version", 2),
                             ("project_binding", "wrong-project"),
                             ("smtp_user", "wrong@example.com"), ("apify_token", None),
                             ("deepseek_api_key", ["value"]),
                             ("smtp_password", "embedded\nnewline"),
                             ("unexpected", "field")):
            values = dict(valid)
            values[field] = value
            blob = json.dumps(values).encode("utf-8")
            with self.subTest(field=field):
                self.assert_code("CREDENTIAL_STORE_INVALID", lambda:
                                 store.load_credentials(backend=MemoryBackend(blob)))
        for field in valid:
            values = dict(valid)
            del values[field]
            self.assert_code("CREDENTIAL_STORE_INVALID", lambda:
                             store.load_credentials(backend=MemoryBackend(
                                 json.dumps(values).encode("utf-8"))))

    def test_oversized_credentials_rejected_before_backend_creation(self):
        huge = credentials(apify_token="a" * 2400)
        with mock.patch.object(store, "_WindowsCredentialBackend", side_effect=AssertionError):
            self.assert_code("CREDENTIAL_STORE_TOO_LARGE",
                             lambda: store.save_credentials(huge))

    def test_wrong_sender_and_wrong_input_type_do_not_write(self):
        self.assert_code("CREDENTIAL_STORE_INVALID",
                         lambda: self.save(credentials(smtp_user="other@example.com")))
        self.assert_code("CREDENTIAL_STORE_INVALID",
                         lambda: store.save_credentials({"secret": "SYNTHETIC"},
                                                        backend=self.backend))
        self.assertEqual(self.api.calls, [])

    def test_qq_authorization_code_must_have_16_characters(self):
        for password in ("short", "SYNTHETIC-PASSWORD-TOO-LONG"):
            self.assert_code("CREDENTIAL_STORE_INVALID", lambda:
                             self.save(credentials(smtp_password=password)))
        self.assertEqual(self.api.calls, [])

    def test_repr_of_loaded_credentials_and_backend_are_redacted(self):
        self.save()
        result = store.load_credentials(backend=self.backend)
        self.assertEqual(repr(result), "ProjectCredentials(<redacted>)")
        self.assertEqual(repr(self.backend), "_WindowsCredentialBackend(<redacted>)")
        self.assertNotIn("SYNTHETIC", repr(result) + repr(self.backend))

    def test_invalid_diagnostic_code_cannot_echo_a_secret(self):
        self.assertEqual(str(store.CredentialStoreError("SYNTHETIC-SECRET")),
                         "CREDENTIAL_STORE_INVALID")

    def test_win32_signatures_and_structures_include_all_fields(self):
        self.assertEqual(ctypes.sizeof(store._DWORD), 4)
        self.assertEqual(ctypes.sizeof(store._FILETIME), 8)
        self.assertEqual([field[0] for field in store._CREDENTIALW._fields_],
                         ["Flags", "Type", "TargetName", "Comment", "LastWritten",
                          "CredentialBlobSize", "CredentialBlob", "Persist",
                          "AttributeCount", "Attributes", "TargetAlias", "UserName"])
        self.assertEqual(len(self.api.CredReadW.argtypes), 4)
        self.assertEqual(len(self.api.CredWriteW.argtypes), 2)
        self.assertIsNone(self.api.CredFree.restype)

    def test_non_windows_has_no_plaintext_fallback(self):
        with mock.patch.object(store.os, "name", "posix"):
            self.assert_code("CREDENTIAL_STORE_UNAVAILABLE", store.load_credentials)
            self.assert_code("CREDENTIAL_STORE_UNAVAILABLE", store.has_credentials)
            self.assert_code("CREDENTIAL_STORE_UNAVAILABLE", store.delete_credentials)
            self.assert_code("CREDENTIAL_STORE_UNAVAILABLE",
                             lambda: store.save_credentials(credentials()))

    def test_isolated_target_supported_only_through_explicit_backend(self):
        separate = store._WindowsCredentialBackend(
            target_name="CodexResetMonitor:synthetic-isolated-test",
            api=self.api, get_error=lambda: self.api.error)
        store.save_credentials(credentials(), backend=separate)
        self.assertEqual(self.api.saved["target"],
                         "CodexResetMonitor:synthetic-isolated-test")
        self.assertIsNotNone(store.load_credentials(backend=separate))
        self.assertTrue(store.delete_credentials(backend=separate))

    def test_high_level_backend_errors_are_fixed(self):
        backend = mock.Mock()
        for method, operation, code in (
                ("read", store.load_credentials, "CREDENTIAL_STORE_READ_FAILED"),
                ("exists", store.has_credentials, "CREDENTIAL_STORE_READ_FAILED"),
                ("delete", store.delete_credentials, "CREDENTIAL_STORE_DELETE_FAILED")):
            getattr(backend, method).side_effect = RuntimeError("SYNTHETIC-SECRET")
            self.assert_code(code, lambda: operation(backend=backend))
        backend.write.side_effect = RuntimeError("SYNTHETIC-SECRET")
        self.assert_code("CREDENTIAL_STORE_WRITE_FAILED", lambda:
                         store.save_credentials(credentials(), backend=backend))


if __name__ == "__main__":
    unittest.main()
