"""Per-connection driver: the only place in Part A that touches a socket.

The loop below is the whole point of the assignment:

    drain every complete request already in the buffer
    send the answers, in arrival order
    only then, recv() more bytes

Calling ``recv()`` while a complete message is still buffered is the bug this
shape makes unrepresentable -- and it is also, unchanged, the implementation
of pipelining.
"""

from __future__ import annotations

import contextlib
import socket
from dataclasses import dataclass, field

from .http.errors import HttpError, RequestTimeout
from .http.message import Request
from .http.parser import DEFAULT_LIMITS, Limits, RequestParser
from .http.response import Response


@dataclass
class Config:
    idle_timeout: float = 15.0
    # Apache calls this MaxKeepAliveRequests. Without it one client can hold a
    # worker thread open for as long as it likes.
    max_requests_per_connection: int = 1000
    recv_size: int = 65536
    limits: Limits = field(default_factory=lambda: DEFAULT_LIMITS)


class ConnectionHandler:
    def __init__(self, sock: socket.socket, addr, router, config: Config, stats, log):
        self.sock = sock
        self.addr = addr
        self.router = router
        self.config = config
        self.stats = stats
        self.log = log
        self.requests_served = 0

    def run(self) -> None:
        parser = RequestParser(self.config.limits)
        try:
            self.sock.settimeout(self.config.idle_timeout)
            # A request/response protocol on a kept-open connection is exactly
            # the case where Nagle's algorithm and delayed ACK interact badly.
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

            while True:
                batch, should_close = self._drain(parser)
                if batch:
                    self.sock.sendall(b"".join(batch))
                if should_close:
                    return

                try:
                    data = self.sock.recv(self.config.recv_size)
                except TimeoutError:
                    self._on_idle_timeout(parser)
                    return
                except OSError:
                    return

                if not data:
                    # Orderly shutdown from the peer. If they stopped
                    # mid-message, that is their problem, not a 400.
                    return
                parser.feed(data)
        finally:
            self._close()

    def _drain(self, parser: RequestParser) -> tuple[list[bytes], bool]:
        """Every complete request currently in the buffer, answered in order."""
        out: list[bytes] = []
        while True:
            try:
                request = parser.next_request()
            except HttpError as error:
                out.append(Response.from_error(error).serialize())
                self.stats.record_response()
                self.log.info("%s -> %d %s", self._peer(), error.status, error.detail)
                if error.close:
                    # We no longer know where the next message starts, so
                    # whatever is in the buffer is not a message.
                    parser.discard()
                    return out, True
                continue  # framing intact: the next request is right there

            if request is None:
                return out, False

            response = self._handle(request)
            out.append(response.serialize(omit_body=request.method == "HEAD"))
            self.stats.record_response()
            self.log.info(
                "%s %s %s -> %d (%s framing, %d bytes)",
                self._peer(),
                request.method,
                request.target,
                response.status,
                request.framing,
                request.raw_length,
            )
            if response.close:
                return out, True

    def _handle(self, request: Request) -> Response:
        try:
            response = self.router.dispatch(request)
        except HttpError as error:
            response = Response.from_error(error)
        except Exception:  # noqa: BLE001 - a bug here must not kill the server
            self.log.exception("unhandled error serving %s", request.target)
            response = Response(status=500, body=b"500 Internal Server Error")

        self.requests_served += 1
        if request.wants_close:
            response.close = True
        if self.requests_served >= self.config.max_requests_per_connection:
            response.close = True
        return response

    def _on_idle_timeout(self, parser: RequestParser) -> None:
        if not parser.mid_message:
            # A merely idle peer gets hung up on quietly. Sending 408 to a
            # client that is about to reuse the connection just races it.
            self.log.info("%s idle timeout, closing", self._peer())
            return
        error = RequestTimeout("request not completed within the idle timeout")
        self.log.info("%s -> 408 (partial request abandoned)", self._peer())
        try:
            self.sock.sendall(Response.from_error(error).serialize())
            self.stats.record_response()
        except OSError:
            pass

    def _close(self) -> None:
        with contextlib.suppress(OSError):
            # Half-close first so the peer reads our last response instead of
            # an RST that discards it.
            self.sock.shutdown(socket.SHUT_WR)
        with contextlib.suppress(OSError):
            self.sock.close()
        self.stats.record_disconnect()

    def _peer(self) -> str:
        return f"{self.addr[0]}:{self.addr[1]}"
