"""Real wall-clock deadlines through injected connections; no real sockets."""

from collections import deque
import http.client
from types import SimpleNamespace
import threading
import time
import unittest
from unittest.mock import patch

from monitor.deepseek import DeepSeekClassifier
from monitor.http_transport import HttpResponse, StdlibHTTPTransport
from monitor.models import StageFailure


class FakeSocket:
    def __init__(self):
        self.cancelled = threading.Event()
        self.timeouts = []

    def settimeout(self, value):
        self.timeouts.append(value)

    def shutdown(self, how):
        self.cancelled.set()

    def close(self):
        self.cancelled.set()


class ChunkResponse:
    status = 200

    def __init__(self, sock, chunks=(), *, drip=False):
        self.sock, self.chunks, self.drip = sock, deque(chunks), drip
        self.reads = 0
        self.closed = threading.Event()

    def getheaders(self):
        return []

    def read1(self, amount):
        self.reads += 1
        if self.drip:
            if self.sock.cancelled.wait(0.02):
                raise OSError("synthetic read interrupted")
            return b"x"
        return self.chunks.popleft() if self.chunks else b""

    def close(self):
        self.closed.set()


class Connection:
    def __init__(self, *, drip=False, header_wait=False, connect_gate=None, chunks=(b"{}",)):
        self.sock = FakeSocket()
        self.response = ChunkResponse(self.sock, chunks, drip=drip)
        self.posts = []
        self.header_wait = header_wait
        self.connect_gate = connect_gate
        self.connected = threading.Event()
        self.closed = threading.Event()
        self.closed_after_connect = threading.Event()
        self.headers_started = threading.Event()

    def connect(self):
        if self.connect_gate is not None:
            self.connect_gate.wait(2)
        self.connected.set()

    def request(self, method, path, **kwargs):
        self.posts.append((method, path, time.monotonic()))

    def getresponse(self):
        self.headers_started.set()
        if self.header_wait:
            self.sock.cancelled.wait(2)
            raise OSError("synthetic headers interrupted")
        return self.response

    def close(self):
        self.sock.cancelled.set()
        self.closed.set()
        if self.connected.is_set():
            self.closed_after_connect.set()


class HTTPDeadlineTests(unittest.TestCase):
    def setUp(self):
        # Context construction is independently covered by the TLS boundary tests;
        # remove that variable from sub-second scheduling tests.
        context = SimpleNamespace(check_hostname=True, verify_mode=2)
        self.context_patch = patch("monitor.http_transport.ssl.create_default_context", return_value=context)
        self.socket_patch = patch("socket.socket", side_effect=AssertionError("Real network forbidden"))
        self.context_patch.start()
        self.socket_patch.start()
        self.addCleanup(self.socket_patch.stop)
        self.addCleanup(self.context_patch.stop)

    def invoke(self, transport, *, timeout=0.08, deadline=None):
        return transport.request(
            "POST", "https://api.deepseek.com/chat/completions",
            headers={"Authorization": "Bearer synthetic"}, body=b"{}",
            timeout=timeout, deadline=deadline,
        )

    def test_slow_drip_body_cannot_reset_total_wall_clock_timeout(self):
        connection = Connection(drip=True)
        transport = StdlibHTTPTransport(connection_factory=lambda *args, **kwargs: connection)
        started = time.monotonic()
        with self.assertRaises(StageFailure) as caught:
            self.invoke(transport, timeout=0.09)
        elapsed = time.monotonic() - started
        self.assertTrue(caught.exception.retryable)
        self.assertLess(elapsed, 0.45)
        self.assertGreaterEqual(elapsed, 0.06)
        self.assertTrue(connection.closed.wait(0.5))
        self.assertTrue(connection.response.closed.wait(0.5))
        self.assertTrue(connection.sock.cancelled.is_set())
        self.assertGreaterEqual(connection.response.reads, 2)
        self.assertEqual(len(connection.posts), 1)

    def test_blocked_headers_return_at_deadline_and_shutdown_socket(self):
        connection = Connection(header_wait=True)
        transport = StdlibHTTPTransport(connection_factory=lambda *args, **kwargs: connection)
        started = time.monotonic()
        with self.assertRaises(StageFailure):
            self.invoke(transport, timeout=0.07)
        self.assertLess(time.monotonic() - started, 0.45)
        self.assertTrue(connection.headers_started.is_set())
        self.assertTrue(connection.sock.cancelled.wait(0.5))
        self.assertTrue(connection.closed.wait(0.5))
        self.assertEqual(connection.response.reads, 0)

    def test_delayed_factory_is_bounded_and_never_sends_after_return(self):
        release = threading.Event()
        entered = threading.Event()
        connection = Connection()
        self.addCleanup(release.set)
        def factory(*args, **kwargs):
            entered.set()
            release.wait(2)
            return connection
        transport = StdlibHTTPTransport(connection_factory=factory)
        started = time.monotonic()
        with self.assertRaises(StageFailure):
            self.invoke(transport, timeout=0.06)
        self.assertLess(time.monotonic() - started, 0.45)
        self.assertTrue(entered.is_set())
        self.assertEqual(connection.posts, [])
        release.set()
        self.assertTrue(connection.closed.wait(0.5))
        self.assertEqual(connection.posts, [], "A factory returning after the budget must not start POST")

    def test_dns_or_connect_returning_after_deadline_cannot_start_post(self):
        release = threading.Event()
        self.addCleanup(release.set)
        connection = Connection(connect_gate=release)
        transport = StdlibHTTPTransport(connection_factory=lambda *args, **kwargs: connection)
        with self.assertRaises(StageFailure):
            self.invoke(transport, timeout=0.06)
        self.assertFalse(connection.connected.is_set())
        release.set()
        self.assertTrue(connection.closed_after_connect.wait(0.5))
        self.assertEqual(connection.posts, [])

    def test_explicit_deadline_shortens_timeout_and_cannot_extend_it(self):
        for timeout, budget in ((0.2, 0.05), (0.05, 60)):
            connection = Connection(header_wait=True)
            transport = StdlibHTTPTransport(connection_factory=lambda *args, **kwargs: connection)
            started = time.monotonic()
            with self.subTest(timeout=timeout, budget=budget), self.assertRaises(StageFailure):
                self.invoke(transport, timeout=timeout, deadline=started + budget)
            self.assertLess(time.monotonic() - started, 0.45)
            self.assertTrue(connection.closed.wait(0.5))

    def test_expired_deadline_and_invalid_configuration_make_no_connection(self):
        calls = []
        transport = StdlibHTTPTransport(connection_factory=lambda *args, **kwargs: calls.append(1))
        with self.assertRaises(StageFailure):
            self.invoke(transport, deadline=time.monotonic() - 1)
        for change in ({"timeout": 61}, {"timeout": True}, {"timeout": float("nan")},
                       {"deadline": float("nan")}, {"deadline": True}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.invoke(transport, **change)
        self.assertEqual(calls, [])

    def test_cumulative_stage_time_uses_one_injected_monotonic_deadline(self):
        clock = [100.0]
        connection = Connection(chunks=(b"a", b"b"))
        def factory(*args, **kwargs):
            clock[0] += 2
            return connection
        original_connect = connection.connect
        def connect():
            clock[0] += 3
            original_connect()
        connection.connect = connect
        original_request = connection.request
        def request(*args, **kwargs):
            clock[0] += 3
            original_request(*args, **kwargs)
        connection.request = request
        original_read = connection.response.read1
        def read(amount):
            clock[0] += 3
            return original_read(amount)
        connection.response.read1 = read
        transport = StdlibHTTPTransport(connection_factory=factory, monotonic=lambda: clock[0])
        with self.assertRaises(StageFailure):
            self.invoke(transport, timeout=20, deadline=110)
        self.assertEqual(connection.response.reads, 1)
        self.assertEqual(len(connection.posts), 1)

    def test_standard_http_send_stops_between_headers_and_request_body(self):
        clock = [100.0]
        class SendingSocket(FakeSocket):
            def __init__(inner):
                super().__init__()
                inner.sent = []
            def sendall(inner, data):
                inner.sent.append(data)
                clock[0] = 105.0  # Budget expires during the header write.
        sock = SendingSocket()
        class StandardConnection(http.client.HTTPConnection):
            def connect(inner):
                inner.sock = sock
            def getresponse(inner):
                self.fail("Expired request must not proceed to response headers")
        connection = StandardConnection("api.deepseek.com")
        transport = StdlibHTTPTransport(
            connection_factory=lambda *args, **kwargs: connection,
            monotonic=lambda: clock[0],
        )
        with self.assertRaises(StageFailure):
            self.invoke(transport, timeout=20, deadline=105)
        self.assertEqual(len(sock.sent), 1)
        self.assertTrue(sock.sent[0].startswith(b"POST /chat/completions HTTP/1.1\r\n"))
        self.assertNotIn(b"{}", sock.sent)
        self.assertEqual(connection.auto_open, 0, "A cancelled socket must never trigger an implicit reconnect")

    def test_oversized_stdlib_branch_and_fake_response_have_same_permanent_failure(self):
        from datetime import datetime, timezone
        from tests.test_deepseek import QueueTransport, tweet
        chunks = [b"x" * 65_536] * 16 + [b"z"]
        connection = Connection(chunks=chunks)
        stdlib = StdlibHTTPTransport(connection_factory=lambda *args, **kwargs: connection)
        fake = QueueTransport(HttpResponse(200, b"x" * 1_048_577))
        for transport in (stdlib, fake):
            adapter = DeepSeekClassifier("synthetic", transport)
            with self.subTest(transport=type(transport).__name__), self.assertRaises(StageFailure) as caught:
                adapter.classify(tweet(), now=datetime(2026, 10, 10, tzinfo=timezone.utc), timezone="Asia/Shanghai")
            self.assertFalse(caught.exception.retryable)
        self.assertEqual(len(connection.posts), 1)
        self.assertEqual(connection.response.reads, 17)


if __name__ == "__main__":
    unittest.main()
