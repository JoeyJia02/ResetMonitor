"""Offline API setup boundary checks; all service responses are synthetic."""

from contextlib import ExitStack
import getpass
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request
import warnings

from scripts import verify_api_connections as probe


class FakeResponse:
    def __init__(self, body, status=200):
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.status = status
        self.read_limits = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.closed = True

    def getcode(self):
        return self.status

    def read(self, limit):
        self.read_limits.append(limit)
        return self.body[:limit]


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, request, *, timeout):
        self.calls.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def apify_ok():
    return FakeResponse({"data": {"items": [{"id": "PRIVATE_SYNTHETIC_RUN"}]}})


def deepseek_ok():
    return FakeResponse({"is_available": True, "balance_infos": [
        {"currency": "CNY", "total_balance": "12.00", "ignored": "PRIVATE_SERVER_FIELD"}]})


class APISetupProbeTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name in ("socket.socket", "socket.create_connection"):
            self.stack.enter_context(patch(name, side_effect=AssertionError("REAL_NETWORK_FORBIDDEN")))
        self.stack.enter_context(patch.object(probe.sys, "stdin", SimpleNamespace(isatty=lambda: True)))
        self.stack.enter_context(patch.object(probe.sys, "stderr", SimpleNamespace(isatty=lambda: True)))
        self.output = io.StringIO()
        self.apify_secret = "SYNTHETIC_APIFY_SECRET"
        self.deepseek_secret = "SYNTHETIC_DEEPSEEK_SECRET"
        self.transport = FakeTransport([apify_ok(), deepseek_ok()])

    def run_probe(self, args=None, **overrides):
        secrets = iter([self.apify_secret, self.deepseek_secret])
        options = {"output": self.output, "hidden_input": lambda _: next(secrets),
                   "transport": self.transport}
        options.update(overrides)
        return probe.main(["--check"] if args is None else args, **options)

    def test_preview_has_no_input_environment_transport_or_state_side_effects(self):
        with patch.object(probe, "_build_transport", side_effect=AssertionError("NO_TRANSPORT")), \
             patch.object(probe.getpass, "getpass", side_effect=AssertionError("NO_SECRET")), \
             patch("os.getenv", side_effect=AssertionError("NO_ENV")), \
             patch("builtins.open", side_effect=AssertionError("NO_FILES")):
            for args in ([], ["--preview"], ["--help"]):
                self.assertEqual(self.run_probe(args, hidden_input=lambda _: self.fail("NO_INPUT")), 0)
        self.assertEqual(self.transport.calls, [])
        self.assertIn("PREVIEW_ONLY", self.output.getvalue())
        self.assertIn("token authentication and Run permission remain unverified", self.output.getvalue())
        self.assertIn("does not validate model inference", self.output.getvalue())

    def test_non_tty_refuses_before_secret_input_or_io(self):
        for stream in ("stdin", "stderr"):
            with patch.object(probe.sys, stream, SimpleNamespace(isatty=lambda: False)):
                self.assertEqual(self.run_probe(hidden_input=lambda _: self.fail("NO_INPUT")), 2)
        self.assertEqual(self.transport.calls, [])

    def test_echo_fallback_and_cancel_never_send(self):
        def fallback(_):
            warnings.warn(self.apify_secret, getpass.GetPassWarning)
            self.fail("MUST_NOT_CONTINUE")
        self.assertEqual(self.run_probe(hidden_input=fallback), 2)
        for error in (KeyboardInterrupt(), EOFError(self.deepseek_secret)):
            def cancel(_, failure=error):
                raise failure
            self.assertEqual(self.run_probe(hidden_input=cancel), 2)
        self.assertEqual(self.transport.calls, [])
        self.assertNotIn(self.apify_secret, self.output.getvalue())
        self.assertNotIn(self.deepseek_secret, self.output.getvalue())

    def test_invalid_arguments_and_credentials_are_not_echoed(self):
        self.assertEqual(self.run_probe([self.apify_secret]), 2)
        for invalid in ("", "space secret", "é", "x" * 4097, "secret\n"):
            values = iter([self.apify_secret, invalid])
            self.assertEqual(self.run_probe(hidden_input=lambda _: next(values)), 2)
            if invalid:
                self.assertNotIn(invalid, self.output.getvalue())
        self.assertEqual(self.transport.calls, [])
        self.assertNotIn(self.apify_secret, self.output.getvalue())

    def test_exact_get_routes_bearer_and_only_public_success_fields(self):
        responses = list(self.transport.responses)
        self.assertEqual(self.run_probe(), 0)
        self.assertEqual(len(self.transport.calls), 2)
        for (request, timeout), url, secret in zip(
                self.transport.calls, (probe.APIFY_URL, probe.DEEPSEEK_URL),
                (self.apify_secret, self.deepseek_secret)):
            self.assertEqual(request.full_url, url)
            self.assertEqual(request.get_method(), "GET")
            self.assertIsNone(request.data)
            self.assertEqual(request.get_header("Authorization"), "Bearer " + secret)
            self.assertEqual(timeout, 15)
        for response in responses:
            self.assertTrue(response.closed)
            self.assertEqual(response.read_limits, [probe.MAX_RESPONSE_BYTES + 1])
        output = self.output.getvalue()
        self.assertIn("APIFY_LIST_RUNS_OK", output)
        self.assertIn("DEEPSEEK_BALANCE_OK", output)
        self.assertIn("is_available=true", output)
        self.assertIn("currency=CNY total_balance=12.00", output)
        for private in (self.apify_secret, self.deepseek_secret,
                        "PRIVATE_SYNTHETIC_RUN", "PRIVATE_SERVER_FIELD"):
            self.assertNotIn(private, output)

    def test_http_and_network_errors_do_not_retry_and_other_service_continues(self):
        for failure, expected in (
                (HTTPError(probe.APIFY_URL, 401, self.apify_secret, {}, io.BytesIO(b"PRIVATE_BODY")), "APIFY_HTTP_401"),
                (URLError(self.apify_secret), "APIFY_NETWORK_ERROR")):
            transport = FakeTransport([failure, deepseek_ok()])
            self.assertEqual(self.run_probe(transport=transport), 1)
            self.assertEqual(len(transport.calls), 2)
            self.assertIn(expected, self.output.getvalue())
            self.assertIn("DEEPSEEK_BALANCE_OK", self.output.getvalue())
        self.assertNotIn(self.apify_secret, self.output.getvalue())
        self.assertNotIn("PRIVATE_BODY", self.output.getvalue())

    def test_apify_schema_and_strict_json_fail_closed(self):
        for body in (b"[]", b"{}", b'{"data":{"items":{}}}',
                     b'{"data":{"items":[{},{}]}}', b'{"data":{"items":[],"items":[]}}',
                     b'{"data":{"items":[],"extra":NaN}}', b"INVALID_JSON_PRIVATE"):
            transport = FakeTransport([FakeResponse(body), deepseek_ok()])
            self.assertEqual(self.run_probe(transport=transport), 1)
            self.assertEqual(len(transport.calls), 2)
        self.assertIn("APIFY_RESPONSE_INVALID", self.output.getvalue())
        self.assertNotIn("INVALID_JSON_PRIVATE", self.output.getvalue())

    def test_deepseek_balance_schema_and_nonfinite_values_fail_closed(self):
        for currency, amount in ((self.deepseek_secret, "12"), ("CNY", "NaN"),
                                 ("CNY", "Infinity"), ("CNY", "-1"), ("CNY", 12),
                                 ("CNY", "1e99999999"), ("CNY", "1" * 65),
                                 ("CNY", self.deepseek_secret)):
            bad = FakeResponse({"is_available": True, "balance_infos": [
                {"currency": currency, "total_balance": amount}]})
            transport = FakeTransport([apify_ok(), bad])
            self.assertEqual(self.run_probe(transport=transport), 1)
            self.assertEqual(len(transport.calls), 2)
        self.assertIn("DEEPSEEK_RESPONSE_INVALID", self.output.getvalue())
        self.assertNotIn(self.deepseek_secret, self.output.getvalue())

    def test_response_cap_and_noninteger_status(self):
        oversized = FakeResponse(b"x" * (probe.MAX_RESPONSE_BYTES + 1))
        transport = FakeTransport([oversized, deepseek_ok()])
        self.assertEqual(self.run_probe(transport=transport), 1)
        self.assertEqual(oversized.read_limits, [probe.MAX_RESPONSE_BYTES + 1])
        self.assertTrue(oversized.closed)
        transport = FakeTransport([FakeResponse({}, True), deepseek_ok()])
        self.assertEqual(self.run_probe(transport=transport), 1)
        self.assertIn("APIFY_HTTP_ERROR", self.output.getvalue())

    def test_redirect_handler_refuses_without_following_or_echoing_location(self):
        request = Request(probe.APIFY_URL, headers={"Authorization": "Bearer " + self.apify_secret})
        response = FakeResponse({})
        handler = probe._RejectRedirects()
        with self.assertRaises(HTTPError) as failure:
            handler.http_error_302(request, response, 302, self.apify_secret,
                                   {"Location": "https://other.invalid/" + self.apify_secret})
        transport = FakeTransport([failure.exception, deepseek_ok()])
        self.assertEqual(self.run_probe(transport=transport), 1)
        self.assertEqual(len(transport.calls), 2)
        self.assertTrue(response.closed)
        self.assertIn("APIFY_HTTP_302", self.output.getvalue())
        self.assertNotIn(self.apify_secret, self.output.getvalue())

    def test_interruption_stops_before_other_service(self):
        transport = FakeTransport([KeyboardInterrupt(), deepseek_ok()])
        self.assertEqual(self.run_probe(transport=transport), 2)
        self.assertEqual(len(transport.calls), 1)
        self.assertIn("API_CHECK_CANCELLED", self.output.getvalue())

    def test_default_transport_uses_no_environment_proxy_and_checks_tls(self):
        with patch.object(probe, "build_opener") as factory:
            probe._build_transport()
        handlers = factory.call_args.args
        self.assertEqual(handlers[0].proxies, {})
        self.assertIsInstance(handlers[1], probe._RejectRedirects)
        context = handlers[2]._context
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, probe.ssl.CERT_REQUIRED)


if __name__ == "__main__":
    unittest.main()
