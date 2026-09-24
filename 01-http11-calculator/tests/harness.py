"""Test harness: an in-process server and a client that frames properly.

The client here reads responses by Content-Length rather than by slurping
until EOF, because on a keep-alive connection there is no EOF to slurp until.
It is the same problem as the server's, mirrored -- which is the point.
"""

from __future__ import annotations

import contextlib
import logging
import select
import socket
from dataclasses import dataclass

from calcserver.connection import Config
from calcserver.server import Server

logging.getLogger("calcserver").addHandler(logging.NullHandler())
logging.getLogger("calcserver").propagate = False


class ServerFixture:
    """Starts a server on an ephemeral port; stops it on exit."""

    def __init__(self, **config_kwargs):
        self.server = Server("127.0.0.1", 0, config=Config(**config_kwargs))

    def __enter__(self) -> Server:
        return self.server.start()

    def __exit__(self, *exc) -> None:
        self.server.shutdown()


@dataclass
class HttpResponse:
    status: int
    reason: str
    headers: dict[str, str]
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    @property
    def keep_alive(self) -> bool:
        return self.headers.get("connection", "").lower() != "close"


class PeerClosed(Exception):
    pass


class RawClient:
    """A socket and a response framer. No conveniences on purpose."""

    def __init__(self, address: tuple[str, int], timeout: float = 5.0):
        self.sock = socket.create_connection(address, timeout=timeout)
        self.sock.settimeout(timeout)
        self._buf = bytearray()

    def send(self, data: bytes) -> None:
        self.sock.sendall(data)

    def send_slowly(self, data: bytes, chunk: int = 1) -> None:
        for index in range(0, len(data), chunk):
            self.sock.sendall(data[index : index + chunk])

    def request(self, method: str, target: str, *, host: str | None = "localhost") -> bytes:
        lines = [f"{method} {target} HTTP/1.1"]
        if host is not None:
            lines.append(f"Host: {host}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")

    def get(self, target: str, **kwargs) -> HttpResponse:
        self.send(self.request("GET", target, **kwargs))
        return self.read_response()

    def _fill(self) -> None:
        data = self.sock.recv(65536)
        if not data:
            raise PeerClosed("server closed the connection")
        self._buf += data

    def read_response(self, *, has_body: bool = True) -> HttpResponse:
        """Read one response, framed by Content-Length.

        ``has_body=False`` is for HEAD: the response announces the length of
        a body it is forbidden to send, and a client that waits for those
        bytes deadlocks against a server that is behaving correctly.
        """
        while (index := self._buf.find(b"\r\n\r\n")) == -1:
            self._fill()
        head = bytes(self._buf[:index]).decode("latin-1")
        del self._buf[: index + 4]

        status_line, *field_lines = head.split("\r\n")
        _, _, rest = status_line.partition(" ")
        code, _, reason = rest.partition(" ")
        headers = {}
        for line in field_lines:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()

        length = int(headers.get("content-length", 0)) if has_body else 0
        while len(self._buf) < length:
            self._fill()
        body = bytes(self._buf[:length])
        del self._buf[:length]
        return HttpResponse(int(code), reason, headers, body)

    def read_responses(self, count: int) -> list[HttpResponse]:
        return [self.read_response() for _ in range(count)]

    @property
    def buffered(self) -> int:
        return len(self._buf)

    def is_open(self, wait: float = 0.3) -> bool:
        """True if the server has not hung up.

        A closed connection shows up as "readable, and reads zero bytes".
        Readable-with-data means it is open and has something for us.
        """
        readable, _, _ = select.select([self.sock], [], [], wait)
        if not readable:
            return True
        return bool(self.sock.recv(1, socket.MSG_PEEK))

    def wait_until_closed(self, timeout: float = 5.0) -> bool:
        self.sock.settimeout(timeout)
        try:
            while True:
                if not self.sock.recv(65536):
                    return True
        except (TimeoutError, OSError):
            return False

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.sock.close()

    def __enter__(self) -> RawClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
