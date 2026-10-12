"""SMTP protocol tests use only in-memory sessions; real connections are forbidden."""

from collections import deque
from dataclasses import replace
from email import policy
from email.parser import BytesParser
import os
import smtplib
import ssl
import unittest
from unittest.mock import patch

from monitor.smtp_transport import SMTPConfig, SMTPNotifier, stdlib_smtp_factory


def configuration(**changes):
    values = dict(host="smtp.example.invalid", port=465,
                  sender="sender@example.invalid", user="test-user",
                  password="fake-authorization-code", recipient="owner@example.invalid",
                  security="SSL", timeout_seconds=7)
    values.update(changes)
    return SMTPConfig(**values)


def notification(**changes):
    values = dict(recipient="owner@example.invalid", subject="【已重置】Codex Reset 已确认",
                  body="完整原文\n.line\n.\n..two\nend\n",
                  message_id="<stable-key@example.invalid>",
                  created_at="2026-10-10T00:00:00Z")
    values.update(changes)
    return values


class FakeSession:
    DEFAULTS = {"ehlo": (250, b"fake hello"), "starttls": (220, b"fake TLS"),
                "auth": (235, b"fake authenticated"), "mail": (250, b"fake sender"),
                "rcpt": (250, b"fake recipient"), "docmd": (354, b"fake data"),
                "getreply": (250, b"fake queued"), "quit": (221, b"fake bye")}

    def __init__(self, **outcomes):
        self.outcomes = {name: deque(values if isinstance(values, list) else [values])
                         for name, values in outcomes.items()}
        self.calls = []
        self.payloads = []
        self.auth_responses = []

    def invoke(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        queue = self.outcomes.get(name)
        outcome = queue.popleft() if queue else self.DEFAULTS.get(name)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def set_debuglevel(self, level):
        return self.invoke("set_debuglevel", level)

    def ehlo(self):
        return self.invoke("ehlo")

    def starttls(self, *, context):
        return self.invoke("starttls", context=context)

    def auth(self, mechanism, authobject, *, initial_response_ok):
        self.auth_responses.append(authobject(b"first challenge"))
        if mechanism == "LOGIN":
            self.auth_responses.append(authobject(b"second challenge"))
        return self.invoke("auth", mechanism, initial_response_ok=initial_response_ok)

    def mail(self, sender):
        return self.invoke("mail", sender)

    def rcpt(self, recipient):
        return self.invoke("rcpt", recipient)

    def docmd(self, command):
        return self.invoke("docmd", command)

    def send(self, payload):
        self.payloads.append(payload)
        return self.invoke("send", payload)

    def getreply(self):
        return self.invoke("getreply")

    def quit(self):
        return self.invoke("quit")

    def close(self):
        return self.invoke("close")

    def names(self):
        return [call[0] for call in self.calls]


class FakeFactory:
    def __init__(self, session=None, error=None):
        self.session = session if session is not None else FakeSession()
        self.error = error
        self.calls = []

    def __call__(self, host, port, **kwargs):
        self.calls.append((host, port, kwargs))
        if self.error is not None:
            raise self.error
        return self.session


class NoEnvironment:
    """Any configuration lookup would fail without touching real credentials."""

    def __getitem__(self, key):
        raise AssertionError("SMTP_MUST_NOT_READ_ENVIRONMENT")

    def get(self, key, default=None):
        raise AssertionError("SMTP_MUST_NOT_READ_ENVIRONMENT")

    def __iter__(self):
        raise AssertionError("SMTP_MUST_NOT_READ_ENVIRONMENT")

    def __contains__(self, key):
        raise AssertionError("SMTP_MUST_NOT_READ_ENVIRONMENT")


class SMTPTransportTests(unittest.TestCase):
    def setUp(self):
        for target in ("socket.socket", "socket.create_connection", "smtplib.SMTP", "smtplib.SMTP_SSL"):
            guard = patch(target, side_effect=AssertionError("REAL_SMTP_FORBIDDEN"))
            guard.start()
            self.addCleanup(guard.stop)
        # Construct a local TLS context without reading system trust paths or env.
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context_patch = patch("monitor.smtp_transport.ssl.create_default_context", return_value=self.context)
        self.context_factory = context_patch.start()
        self.addCleanup(context_patch.stop)

    def deliver(self, session=None, config=None, snapshot=None, error=None):
        factory = FakeFactory(session, error)
        adapter = SMTPNotifier(config or configuration(), smtp_factory=factory)
        result = adapter.send(notification() if snapshot is None else snapshot)
        return result, factory

    def test_factory_is_mandatory_and_construction_has_no_side_effect(self):
        config = configuration()
        with self.assertRaises(TypeError):
            SMTPNotifier(config)
        with self.assertRaises(TypeError):
            SMTPNotifier(config, smtp_factory=None)
        with self.assertRaises(TypeError):
            SMTPNotifier({}, smtp_factory=FakeFactory())
        factory = FakeFactory()
        SMTPNotifier(config, smtp_factory=factory)
        self.assertEqual(factory.calls, [])
        self.context_factory.assert_not_called()

    def test_no_environment_reads_during_construction_or_send(self):
        with patch.object(os, "environ", NoEnvironment()):
            result, factory = self.deliver()
        self.assertEqual(result.status, "SENT")
        self.assertEqual(len(factory.calls), 1)

    def test_SSL_secure_context_timeout_and_explicit_single_envelope(self):
        session = FakeSession()
        result, factory = self.deliver(session)
        self.assertEqual(result.status, "SENT")
        self.assertFalse(result.retryable)
        self.assertEqual(factory.calls, [("smtp.example.invalid", 465,
                                        {"timeout": 7, "context": self.context})])
        self.assertTrue(self.context.check_hostname)
        self.assertEqual(self.context.verify_mode, ssl.CERT_REQUIRED)
        self.assertGreaterEqual(self.context.minimum_version, ssl.TLSVersion.TLSv1_2)
        self.assertEqual(session.names(), ["set_debuglevel", "ehlo", "auth", "mail", "rcpt",
                                          "docmd", "send", "getreply", "quit", "close"])
        self.assertEqual(session.calls[0], ("set_debuglevel", (0,), {}))
        self.assertEqual(session.calls[3], ("mail", ("sender@example.invalid",), {}))
        self.assertEqual(session.calls[4], ("rcpt", ("owner@example.invalid",), {}))
        self.assertEqual(session.calls[5], ("docmd", ("DATA",), {}))

    def test_STARTTLS_precedes_auth_and_requires_second_EHLO(self):
        session = FakeSession()
        result, factory = self.deliver(session, configuration(security="STARTTLS", port=587))
        self.assertEqual(result.status, "SENT")
        self.assertEqual(factory.calls, [("smtp.example.invalid", 587, {"timeout": 7})])
        self.assertEqual(session.names()[:5], ["set_debuglevel", "ehlo", "starttls", "ehlo", "auth"])
        self.assertIs(session.calls[2][2]["context"], self.context)
        self.assertTrue(self.context.check_hostname)
        self.assertEqual(self.context.verify_mode, ssl.CERT_REQUIRED)

    def test_AUTH_uses_one_selected_mechanism_no_login_fallback(self):
        for mechanism, responses in (("PLAIN", ["\0test-user\0fake-authorization-code"]),
                                     ("LOGIN", ["test-user", "fake-authorization-code"])):
            with self.subTest(mechanism=mechanism):
                session = FakeSession()
                result, _ = self.deliver(session, configuration(auth_mechanism=mechanism))
                self.assertEqual(result.status, "SENT")
                self.assertEqual(session.auth_responses, responses)
                self.assertEqual(session.names().count("auth"), 1)
                auth_call = next(call for call in session.calls if call[0] == "auth")
                self.assertEqual(auth_call, ("auth", (mechanism,), {"initial_response_ok": False}))

    def test_wire_MIME_fixed_headers_stable_id_date_and_dot_stuffing(self):
        snapshot = notification(headers={"Bcc": "other@example.invalid"},
                                sender="forged@example.invalid", cc="other@example.invalid")
        session = FakeSession()
        result, _ = self.deliver(session, snapshot=snapshot)
        self.assertEqual(result.status, "SENT")
        wire = session.payloads[0]
        wire.decode("ascii")
        self.assertTrue(wire.endswith(b"\r\n.\r\n"))
        self.assertEqual(wire.count(b"\r\n.\r\n"), 1)
        self.assertIn(b"\r\n..line\r\n..\r\n...two\r\n", wire)
        self.assertNotIn(b"\n", wire.replace(b"\r\n", b""))
        restored = wire[:-3].replace(b"\r\n..", b"\r\n.")
        parsed = BytesParser(policy=policy.default).parsebytes(restored)
        self.assertEqual(str(parsed["From"]), "sender@example.invalid")
        self.assertEqual(str(parsed["To"]), "owner@example.invalid")
        self.assertEqual(str(parsed["Subject"]), snapshot["subject"])
        self.assertEqual(str(parsed["Message-ID"]), snapshot["message_id"])
        self.assertEqual(parsed["Date"].datetime.isoformat(), "2026-10-10T00:00:00+00:00")
        self.assertEqual(parsed["Content-Transfer-Encoding"], "quoted-printable")
        self.assertEqual(parsed.get_content().replace("\r\n", "\n"), snapshot["body"])
        self.assertIsNone(parsed["Bcc"])
        self.assertIsNone(parsed["Cc"])

    def test_same_snapshot_has_identical_bytes_but_does_not_claim_deduplication(self):
        session = FakeSession()
        adapter = SMTPNotifier(configuration(), smtp_factory=FakeFactory(session))
        self.assertEqual(adapter.send(notification()).status, "SENT")
        self.assertEqual(adapter.send(notification()).status, "SENT")
        self.assertEqual(len(session.payloads), 2)
        self.assertEqual(session.payloads[0], session.payloads[1])

    def test_invalid_snapshot_rejected_before_connect(self):
        invalid = [None, [], {}, notification(recipient="other@example.invalid"),
                   notification(recipient="owner@example.invalid,other@example.invalid"),
                   notification(subject="allowed\r\nBcc: other@example.invalid"),
                   notification(subject="bad\x00header"), notification(subject="\tbad"),
                   notification(subject=""), notification(message_id="<ok@example.invalid>\nBcc: bad"),
                   notification(message_id="two@example.invalid"),
                   notification(message_id="<one@example.invalid>,<two@example.invalid>"),
                   notification(body=None), notification(body="invalid surrogate \ud800"),
                   notification(created_at="2026-10-10T00:00:00"),
                   notification(created_at="2026-10-10T00:00:00+00:60")]
        for index, snapshot in enumerate(invalid):
            with self.subTest(case=index):
                factory = FakeFactory()
                result = SMTPNotifier(configuration(), smtp_factory=factory).send(snapshot)
                self.assertEqual(result.status, "FAILED")
                self.assertFalse(result.retryable)
                self.assertEqual(result.error_code, "SMTP_INVALID_MESSAGE")
                self.assertEqual(factory.calls, [])

    def test_final_DATA_250_remains_SENT_when_QUIT_and_close_raise(self):
        session = FakeSession(quit=OSError("secret cleanup detail"),
                              close=RuntimeError("secret cleanup detail"))
        result, _ = self.deliver(session)
        self.assertEqual(result.status, "SENT")
        self.assertFalse(result.retryable)
        self.assertIsNone(result.error_code)
        self.assertEqual(session.names()[-2:], ["quit", "close"])

    def test_final_DATA_explicit_refusals_are_known_and_never_retried(self):
        for code, retryable in ((421, True), (450, True), (451, True), (452, True),
                                (500, False), (550, False), (552, False), (554, False)):
            with self.subTest(code=code):
                session = FakeSession(getreply=(code, b"untrusted server detail"))
                result, factory = self.deliver(session)
                self.assertEqual(result.status, "FAILED")
                self.assertEqual(result.retryable, retryable)
                self.assertEqual(result.error_code, "SMTP_DATA_REJECTED")
                self.assertEqual(len(factory.calls), 1)
                self.assertEqual(session.names().count("send"), 1)
                self.assertEqual(session.names().count("getreply"), 1)
                self.assertNotIn("quit", session.names())
                self.assertEqual(session.names()[-1], "close")

    def test_send_exception_is_UNKNOWN_even_if_transmission_might_be_partial(self):
        for error in (TimeoutError("secret"), ConnectionResetError("secret"),
                      smtplib.SMTPServerDisconnected("secret"), RuntimeError("secret")):
            with self.subTest(kind=type(error).__name__):
                session = FakeSession(send=error, close=OSError("secret"))
                result, factory = self.deliver(session)
                self.assertEqual(result.status, "UNKNOWN")
                self.assertEqual(result.error_code, "SMTP_DATA_UNCERTAIN")
                self.assertEqual(len(factory.calls), 1)
                self.assertEqual(session.names().count("send"), 1)
                self.assertNotIn("getreply", session.names())
                self.assertNotIn("quit", session.names())
                self.assertEqual(session.names()[-1], "close")

    def test_final_reply_exception_is_UNKNOWN_including_local_response_exception(self):
        for error in (TimeoutError("secret"), smtplib.SMTPServerDisconnected("secret"),
                      smtplib.SMTPResponseException(500, b"locally generated malformed reply")):
            with self.subTest(kind=type(error).__name__):
                session = FakeSession(getreply=error)
                result, _ = self.deliver(session)
                self.assertEqual(result.status, "UNKNOWN")
                self.assertEqual(result.error_code, "SMTP_DATA_UNCERTAIN")
                self.assertEqual(session.names().count("send"), 1)
                self.assertEqual(session.names().count("getreply"), 1)
                self.assertNotIn("quit", session.names())

    def test_unrecognizable_final_response_is_UNKNOWN(self):
        for reply in ((251, b"unexpected"), (354, b"unexpected"), None, (True, b"bad"),
                      (250,), [250, b"list"], (999, b"bad"), ("250", b"bad")):
            with self.subTest(reply=repr(reply)):
                result, _ = self.deliver(FakeSession(getreply=reply))
                self.assertEqual(result.status, "UNKNOWN")
                self.assertEqual(result.error_code, "SMTP_DATA_UNCERTAIN")

    def test_pre_DATA_refusal_classification_and_stops_before_body(self):
        for phase, code, retryable in (("ehlo", 421, True), ("ehlo", 550, False),
                                      ("auth", 454, True), ("auth", 535, False),
                                      ("mail", 450, True), ("mail", 550, False),
                                      ("rcpt", 450, True), ("rcpt", 550, False),
                                      ("docmd", 451, True), ("docmd", 554, False)):
            with self.subTest(phase=phase, code=code):
                session = FakeSession(**{phase: (code, b"secret server detail")})
                result, factory = self.deliver(session)
                self.assertEqual(result.status, "FAILED")
                self.assertEqual(result.retryable, retryable)
                self.assertEqual(len(factory.calls), 1)
                self.assertNotIn("send", session.names())
                self.assertNotIn("getreply", session.names())
                self.assertEqual(session.names()[-2:], ["quit", "close"])
                self.assertNotIn("secret", repr(result))

    def test_pre_DATA_timeouts_known_FAILED_and_cleanup_cannot_override(self):
        for phase in ("ehlo", "auth", "mail", "rcpt", "docmd"):
            with self.subTest(phase=phase):
                session = FakeSession(**{phase: TimeoutError("secret"),
                                         "quit": OSError("secret"), "close": OSError("secret")})
                result, _ = self.deliver(session)
                self.assertEqual(result.status, "FAILED")
                self.assertTrue(result.retryable)
                self.assertNotIn("send", session.names())
                self.assertNotIn("secret", repr(result))

    def test_connection_failures_are_known_and_factory_called_once(self):
        for error, retryable in ((TimeoutError("secret"), True),
                                 (ConnectionRefusedError("secret"), True),
                                 (ssl.SSLCertVerificationError("secret"), False),
                                 (ssl.SSLError("secret"), False),
                                 (RuntimeError("secret"), False)):
            with self.subTest(kind=type(error).__name__):
                result, factory = self.deliver(error=error)
                self.assertEqual(result.status, "FAILED")
                self.assertEqual(result.retryable, retryable)
                self.assertEqual(len(factory.calls), 1)
                self.assertEqual(factory.session.calls, [])
                self.assertNotIn("secret", repr(result))

    def test_STARTTLS_failure_never_authenticates_or_sends(self):
        for outcome, retryable in (((454, b"temporary"), True),
                                   ((550, b"permanent"), False),
                                   (ssl.SSLCertVerificationError("secret"), False),
                                   (smtplib.SMTPNotSupportedError("secret"), False),
                                   (TimeoutError("secret"), True)):
            with self.subTest(kind=type(outcome).__name__):
                session = FakeSession(starttls=outcome)
                result, _ = self.deliver(session, configuration(security="STARTTLS", port=587))
                self.assertEqual(result.status, "FAILED")
                self.assertEqual(result.retryable, retryable)
                self.assertNotIn("auth", session.names())
                self.assertNotIn("mail", session.names())
                self.assertNotIn("send", session.names())

    def test_second_EHLO_failure_stops_before_credentials(self):
        session = FakeSession(ehlo=[(250, b"first"), (550, b"second")])
        result, _ = self.deliver(session, configuration(security="STARTTLS", port=587))
        self.assertEqual(result.status, "FAILED")
        self.assertFalse(result.retryable)
        self.assertNotIn("auth", session.names())

    def test_auth_exception_codes_535_permanent_454_temporary(self):
        for code, retryable in ((535, False), (454, True)):
            with self.subTest(code=code):
                session = FakeSession(auth=smtplib.SMTPAuthenticationError(code, b"secret"))
                result, _ = self.deliver(session)
                self.assertEqual(result.status, "FAILED")
                self.assertEqual(result.retryable, retryable)
                self.assertEqual(result.error_code, "SMTP_AUTH_REJECTED")
                self.assertEqual(session.names().count("auth"), 1)
                self.assertNotIn("mail", session.names())

    def test_RCPT_251_forwarded_acceptance_and_AUTH_503_already_authenticated(self):
        result, _ = self.deliver(FakeSession(rcpt=(251, b"forwarded"), auth=(503, b"already")))
        self.assertEqual(result.status, "SENT")

    def test_malformed_or_unexpected_pre_DATA_responses_are_permanent(self):
        for phase, reply in (("ehlo", (354, b"wrong phase")), ("auth", (250, b"wrong phase")),
                             ("mail", (251, b"unexpected")), ("rcpt", None),
                             ("docmd", (250, b"no body accepted"))):
            with self.subTest(phase=phase):
                session = FakeSession(**{phase: reply})
                result, _ = self.deliver(session)
                self.assertEqual(result.status, "FAILED")
                self.assertFalse(result.retryable)
                self.assertNotIn("send", session.names())

    def test_stdlib_factory_is_explicit_and_selects_constructor_by_context(self):
        fake = FakeSession()
        with patch("monitor.smtp_transport.smtplib.SMTP_SSL", return_value=fake) as smtp_ssl:
            self.assertIs(stdlib_smtp_factory("smtp.example.invalid", 465, timeout=7,
                                             context=self.context), fake)
            smtp_ssl.assert_called_once_with("smtp.example.invalid", 465, timeout=7, context=self.context)
        with patch("monitor.smtp_transport.smtplib.SMTP", return_value=fake) as smtp:
            self.assertIs(stdlib_smtp_factory("smtp.example.invalid", 587, timeout=7), fake)
            smtp.assert_called_once_with("smtp.example.invalid", 587, timeout=7)


class SMTPConfigTests(unittest.TestCase):
    def test_required_routing_is_explicit(self):
        for required in ("host", "port", "sender", "user", "password", "recipient", "security"):
            with self.subTest(required=required):
                values = dict(host="smtp.example.invalid", port=465, sender="sender@example.invalid",
                              user="test-user", password="fake-code", recipient="owner@example.invalid",
                              security="SSL")
                del values[required]
                with self.assertRaises(TypeError):
                    SMTPConfig(**values)

    def test_invalid_configuration_is_rejected_without_echoing_sensitive_values(self):
        cases = [("host", "smtp.example.invalid\r\nsecret"), ("host", "https://smtp.example.invalid"),
                 ("host", ""), ("host", None), ("port", 0), ("port", 65536), ("port", True),
                 ("port", "465"), ("security", "NONE"), ("security", "ssl"),
                 ("auth_mechanism", "XOAUTH2"), ("sender", "secret\r\nBcc: bad@example.invalid"),
                 ("sender", "Display <sender@example.invalid>"),
                 ("recipient", "one@example.invalid,two@example.invalid"),
                 ("recipient", "one@example.invalid;two@example.invalid"),
                 ("recipient", "é@example.invalid"), ("recipient", ".one@example.invalid"),
                 ("recipient", "one..two@example.invalid"), ("recipient", "one@example..invalid"),
                 ("user", "secret\r\n"), ("user", ""), ("password", "secret\x00"),
                 ("password", "ésecret"), ("timeout_seconds", 0), ("timeout_seconds", -1),
                 ("timeout_seconds", 301), ("timeout_seconds", float("inf")),
                 ("timeout_seconds", float("nan")), ("timeout_seconds", True),
                 ("timeout_seconds", "7"), ("timeout_seconds", 10 ** 1000)]
        for index, (field, value) in enumerate(cases):
            with self.subTest(case=index, field=field):
                with self.assertRaises(ValueError) as raised:
                    configuration(**{field: value})
                self.assertNotIn("secret", str(raised.exception))

    def test_repr_hides_credentials_and_addresses(self):
        config = configuration()
        rendered = repr(config)
        for hidden in (config.sender, config.user, config.password, config.recipient):
            self.assertNotIn(hidden, rendered)
        self.assertEqual(replace(config, timeout_seconds=0.25).timeout_seconds, 0.25)


if __name__ == "__main__":
    unittest.main()
