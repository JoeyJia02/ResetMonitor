"""Explicit HTTP boundary. Importing/constructing it performs no I/O.

Adapters require an injected transport. The standard-library implementation is
available for a future authorized caller, and is never selected by the CLI.
"""
from dataclasses import dataclass, field
import http.client
import math
import socket
import ssl
import threading
import time
from typing import Protocol
from urllib.parse import urlsplit

from .models import StageFailure


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)


class HttpTransport(Protocol):
    def request(self, method: str, url: str, *, headers: dict[str, str],
                body: bytes | None, timeout: float, deadline: float | None = None) -> HttpResponse: ...


class StdlibHTTPTransport:
    """One HTTPS request bounded by an absolute monotonic deadline.

    The main caller waits at most min(timeout, remaining deadline), independent
    of DNS, TLS, headers or slow body delivery. A daemon worker checks cancellation
    between stages and body chunks; timeout requests socket shutdown/close.
    A custom factory cannot be forcibly killed, but a late factory/connect return
    is checked before POST. Arbitrary noncooperative custom methods remain outside
    this implementation's ability to stop their own external side effects.
    """
    def __init__(self, *, connection_factory=None, max_response_bytes=1_048_576,
                 monotonic=time.monotonic):
        if type(max_response_bytes) is not int or not 1 <= max_response_bytes <= 1_048_576:
            raise ValueError("INVALID_RESPONSE_LIMIT")
        self._factory = connection_factory or http.client.HTTPSConnection
        self._max_response_bytes = max_response_bytes
        if not callable(monotonic):
            raise ValueError("INVALID_MONOTONIC_CLOCK")
        self._monotonic = monotonic

    def request(self, method, url, *, headers, body, timeout, deadline=None):
        started = self._monotonic()
        try:
            parts = urlsplit(url)
            if (parts.scheme != "https" or parts.hostname not in {"api.apify.com", "api.deepseek.com"}
                    or parts.port not in (None, 443) or parts.username is not None
                    or parts.password is not None or parts.fragment
                    or any(ord(c) < 33 or ord(c) == 127 for c in url)):
                raise ValueError
            if method not in {"GET", "POST"}:
                raise ValueError
            if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 60:
                raise ValueError
            if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)):
                raise ValueError
            if body is not None and (not isinstance(body, bytes) or len(body) > 1_048_576):
                raise ValueError
            if not isinstance(headers, dict) or any(
                not isinstance(k, str) or not isinstance(v, str) or not k
                or any(ord(c) < 32 or ord(c) == 127 for c in k + v)
                or any(c in k for c in " :\t") for k, v in headers.items()
            ):
                raise ValueError
        except (TypeError, ValueError, AttributeError):
            raise ValueError("INVALID_HTTP_REQUEST") from None
        deadline = min(started + timeout, deadline) if deadline is not None else started + timeout
        cancelled = threading.Event()
        complete = threading.Event()
        shared = {}
        shared_lock = threading.Lock()

        def check():
            remaining = deadline - self._monotonic()
            if cancelled.is_set() or remaining <= 0:
                raise StageFailure(retryable=True)
            return remaining

        def close(connection):
            try:
                current_socket = getattr(connection, "sock", None) or shared.get("socket")
                if current_socket is not None:
                    try:
                        current_socket.shutdown(socket.SHUT_RDWR)
                    except Exception:
                        pass
                connection.close()
            except Exception:
                pass
            response_close = getattr(shared.get("response"), "close", None)
            if callable(response_close):
                try:
                    response_close()
                except Exception:
                    pass

        def set_socket_timeout(connection):
            remaining = check()
            current_socket = getattr(connection, "sock", None)
            if current_socket is not None:
                current_socket.settimeout(min(timeout, remaining))

        def worker():
            connection = None
            try:
                check()
                context = ssl.create_default_context()
                check()
                connection = self._factory(parts.hostname, 443, timeout=timeout, context=context)
                with shared_lock:
                    shared["connection"] = connection
                check()  # A delayed custom factory must never start a late POST.
                connect = getattr(connection, "connect", None)
                if callable(connect):
                    connect()  # Explicit connect isolates DNS/TLS from HTTP send.
                    check()   # DNS/TLS that completes after cancellation cannot send.
                shared["socket"] = getattr(connection, "sock", None)
                if isinstance(connection, http.client.HTTPConnection):
                    # Cancellation may close the socket; forbid automatic reconnect
                    # inside HTTPConnection.send after the explicit connect stage.
                    connection.auto_open = 0
                    original_send = connection.send
                    def deadline_send(data):
                        set_socket_timeout(connection)
                        check()
                        original_send(data)
                        check()
                    connection.send = deadline_send
                set_socket_timeout(connection)
                check()
                path = parts.path or "/"
                if parts.query:
                    path += "?" + parts.query
                connection.request(method, path, body=body, headers=dict(headers))
                check()
                set_socket_timeout(connection)
                response = connection.getresponse()
                shared["response"] = response
                check()
                if type(response.status) is not int or not 100 <= response.status <= 599:
                    raise StageFailure(retryable=False)
                response_headers = dict(response.getheaders())
                check()
                chunks, size = [], 0
                reader = getattr(response, "read1", None)
                if callable(reader):
                    # read1 returns available buffered data after at most one raw
                    # read. Slow-drip delivery cannot reset the overall deadline.
                    while True:
                        set_socket_timeout(connection)
                        check()
                        chunk = reader(min(65_536, self._max_response_bytes + 1 - size))
                        check()
                        if not isinstance(chunk, bytes):
                            raise StageFailure(retryable=False)
                        size += len(chunk)
                        if size > self._max_response_bytes:
                            raise StageFailure(retryable=False)
                        if not chunk:
                            break
                        chunks.append(chunk)
                else:
                    # Compatibility for finite, one-shot injected response fakes.
                    # The main wait still bounds a blocked custom read method.
                    set_socket_timeout(connection)
                    chunk = response.read(self._max_response_bytes + 1)
                    check()
                    if not isinstance(chunk, bytes) or len(chunk) > self._max_response_bytes:
                        raise StageFailure(retryable=False)
                    chunks.append(chunk)
                check()
                shared["result"] = HttpResponse(response.status, b"".join(chunks), response_headers)
            except StageFailure as exc:
                shared["failure"] = (exc.retryable, exc.retry_after_seconds)
            except Exception:
                shared["failure"] = (True, None)
            finally:
                if connection is not None:
                    close(connection)
                complete.set()

        check()  # An expired caller budget does not construct a worker or connection.
        threading.Thread(target=worker, name="reset-monitor-http", daemon=True).start()
        finished = complete.wait(max(0.0, deadline - self._monotonic()))
        if not finished or self._monotonic() >= deadline:
            cancelled.set()
            with shared_lock:
                connection = shared.get("connection")
            if connection is not None:
                # Custom shutdown/close implementations may block: cleanup must
                # never turn the main caller's deadline into another unbounded wait.
                threading.Thread(target=close, args=(connection,), name="reset-monitor-http-cancel", daemon=True).start()
            raise StageFailure(retryable=True)
        if "failure" in shared:
            retryable, retry_after = shared["failure"]
            raise StageFailure(retryable=retryable, retry_after_seconds=retry_after) from None
        return shared["result"]
