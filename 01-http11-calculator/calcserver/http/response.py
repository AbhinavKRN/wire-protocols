"""Response serialisation.

Every response this server emits carries a Content-Length. That is the same
promise we demand of clients: the receiver must be able to find the end of
this message without waiting for the connection to close, because the
connection is not going to close.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from email.utils import formatdate

from .errors import HttpError

SERVER_TOKEN = "calcserver/1.0"

REASONS = {
    200: "OK",
    400: "Bad Request",
    404: "Not Found",
    405: "Method Not Allowed",
    408: "Request Timeout",
    413: "Content Too Large",
    414: "URI Too Long",
    431: "Request Header Fields Too Large",
    500: "Internal Server Error",
    501: "Not Implemented",
    505: "HTTP Version Not Supported",
}


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    content_type: str = "text/plain; charset=utf-8"
    headers: list[tuple[str, str]] = field(default_factory=list)
    close: bool = False
    reason: str | None = None

    @classmethod
    def from_error(cls, error: HttpError) -> Response:
        return cls(
            status=error.status,
            body=error.body(),
            headers=list(error.headers),
            close=error.close,
            reason=error.reason,
        )

    def serialize(self, *, omit_body: bool = False) -> bytes:
        reason = self.reason or REASONS.get(self.status, "Unknown")
        lines = [f"HTTP/1.1 {self.status} {reason}"]
        lines.append(f"Date: {formatdate(usegmt=True)}")
        lines.append(f"Server: {SERVER_TOKEN}")
        lines.append(f"Content-Type: {self.content_type}")
        # Announced even for HEAD, where it describes the body we are not
        # sending; that is what makes HEAD useful.
        lines.append(f"Content-Length: {len(self.body)}")
        lines.append(f"Connection: {'close' if self.close else 'keep-alive'}")
        for name, value in self.headers:
            lines.append(f"{name}: {value}")
        head = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
        return head if omit_body else head + self.body
