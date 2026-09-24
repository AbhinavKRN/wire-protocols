"""Incremental HTTP/1.1 request parser.

There is not a single socket call in this module, and that is deliberate: the
hard part of HTTP/1.1 is not I/O, it is deciding where one message stops. That
decision is a pure function of the bytes received so far, so it is written and
tested as one.

Contract
--------
``feed()`` appends whatever ``recv()`` happened to hand you -- a byte, a
packet, three requests at once. ``next_request()`` returns the next *complete*
message and consumes exactly its bytes, or returns ``None`` to mean "the
answer is not knowable yet, get more data". Byte n+1 belongs to somebody else
and is left in the buffer untouched.

The caller's loop is therefore forced into the correct shape: drain
``next_request()`` until it returns ``None``, and only then call ``recv()``
again. Pipelining is not a feature that gets added later; it is what that loop
does when two requests show up in the same packet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import unquote

from .errors import (
    BadRequest,
    HeaderFieldsTooLarge,
    HttpError,
    MalformedMessage,
    NotImplementedError_,
    PayloadTooLarge,
    UriTooLong,
    VersionNotSupported,
)
from .message import Headers, Request

CRLF = b"\r\n"
HEAD_TERMINATOR = b"\r\n\r\n"

# RFC 9110 tchar. Anything outside this in a method or field name is a
# protocol violation, not an oddity to be tolerated.
TCHAR = frozenset(
    b"!#$%&'*+-.^_`|~0123456789"
    b"abcdefghijklmnopqrstuvwxyz"
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZ"
)

KNOWN_METHODS = frozenset(
    {"GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "TRACE", "CONNECT"}
)


@dataclass(frozen=True)
class Limits:
    """Every one of these is a denial-of-service bound, not a style choice.

    A parser without limits will happily buffer bytes until the process dies,
    on the word of a peer who has not even sent a complete request yet.
    """

    max_request_line: int = 8 * 1024
    max_head: int = 16 * 1024
    max_header_count: int = 100
    max_body: int = 1024 * 1024
    max_chunk_line: int = 1024
    max_leading_blank_lines: int = 8


DEFAULT_LIMITS = Limits()


@dataclass
class _Head:
    """A request line plus headers, parsed, with its body still on the wire."""

    method: str
    target: str
    version: str
    headers: Headers
    path: str
    query_string: str
    framing: str
    content_length: int
    head_bytes: int
    # A semantic error found while parsing the head (a missing Host, say).
    # It is *not* raised yet: the body still has to be drained off the socket
    # first, or the stream is left misaligned and the next request -- and every
    # request after it -- is parsed from the middle of this one's body.
    deferred_error: HttpError | None = None
    trailers: Headers = field(default_factory=Headers)


class RequestParser:
    """Feed it bytes, take complete requests out."""

    def __init__(self, limits: Limits = DEFAULT_LIMITS):
        self.limits = limits
        self._buf = bytearray()
        self._scan = 0  # how far into _buf we have already looked for CRLFCRLF
        self._head: _Head | None = None
        self._blank_lines = 0
        self._head_bytes = 0

    # -- input ------------------------------------------------------------

    def feed(self, data: bytes) -> None:
        self._buf += data

    @property
    def buffered(self) -> int:
        return len(self._buf)

    @property
    def mid_message(self) -> bool:
        """True if a peer has started a request but not finished it."""
        return self._head is not None or bool(self._buf)

    def discard(self) -> None:
        self._buf.clear()
        self._scan = 0
        self._head = None

    # -- output -----------------------------------------------------------

    def next_request(self) -> Request | None:
        """One complete request, or None if more bytes are needed.

        Raises HttpError. Whether the connection survives that error is the
        error's own ``close`` attribute, not this function's business.
        """
        if self._head is None:
            head_bytes = self._take_head()
            if head_bytes is None:
                return None
            self._head = self._parse_head(head_bytes)

        head = self._head
        body = self._take_body(head)
        if body is None:
            return None

        self._head = None
        if head.deferred_error is not None:
            # Body is consumed, stream is aligned again; now it is safe to fail.
            raise head.deferred_error

        return Request(
            method=head.method,
            target=head.target,
            version=head.version,
            headers=head.headers,
            body=body,
            path=head.path,
            query_string=head.query_string,
            framing=head.framing,
            raw_length=head.head_bytes + len(body),
            trailers=head.trailers,
        )

    # -- head -------------------------------------------------------------

    def _take_head(self) -> bytes | None:
        # RFC 9112 3.5: a server should ignore at least one empty line before
        # the request line. Bounded, because "at least one" is not "forever".
        while self._buf[:2] == CRLF:
            self._blank_lines += 1
            if self._blank_lines > self.limits.max_leading_blank_lines:
                raise MalformedMessage("too many blank lines before request line")
            del self._buf[:2]
            self._scan = 0

        start = max(0, self._scan - (len(HEAD_TERMINATOR) - 1))
        index = self._buf.find(HEAD_TERMINATOR, start)

        # A peer using bare LF as a line ending will never send our
        # terminator, so without this it would sit here until the idle
        # timeout. Fail it the moment the evidence arrives instead. Only the
        # head can be in the buffer at this point -- no body bytes precede
        # the head terminator -- so an LF LF here is unambiguous.
        lf_index = self._buf.find(b"\n\n", start)
        if lf_index != -1 and (index == -1 or lf_index < index):
            raise MalformedMessage("bare LF used as a line terminator")

        if index == -1:
            if len(self._buf) > self.limits.max_head:
                raise HeaderFieldsTooLarge(f"head exceeds {self.limits.max_head} bytes")
            self._scan = len(self._buf)
            return None

        if index > self.limits.max_head:
            raise HeaderFieldsTooLarge(f"head exceeds {self.limits.max_head} bytes")

        head = bytes(self._buf[:index])
        del self._buf[: index + len(HEAD_TERMINATOR)]
        self._scan = 0
        self._blank_lines = 0
        self._head_bytes = index + len(HEAD_TERMINATOR)
        return head

    def _parse_head(self, head: bytes) -> _Head:
        lines = head.split(CRLF)
        for line in lines:
            # split() on CRLF leaves any bare CR or bare LF embedded in a line.
            # Accepting those is how two intermediaries end up disagreeing
            # about where a message ended, which is request smuggling.
            if b"\n" in line or b"\r" in line:
                raise MalformedMessage("bare CR or LF in head")

        method, target, version = self._parse_request_line(lines[0])
        headers = self._parse_fields(lines[1:])

        deferred: HttpError | None = None

        # RFC 9112 3.2: HTTP/1.1 requires exactly one Host.
        host_count = headers.count("host")
        if host_count > 1:
            # Two Hosts is an attack, not a mistake: hang up.
            raise MalformedMessage("multiple Host headers")
        if host_count == 0 and version == "HTTP/1.1":
            # One Host short, but perfectly framed. Answer 400 and stay up.
            deferred = BadRequest("HTTP/1.1 request without a Host header")

        framing, content_length = self._decide_framing(headers)
        path, query_string = _split_target(target)

        return _Head(
            method=method,
            target=target,
            version=version,
            headers=headers,
            path=path,
            query_string=query_string,
            framing=framing,
            content_length=content_length,
            head_bytes=self._head_bytes,
            deferred_error=deferred,
        )

    def _parse_request_line(self, line: bytes) -> tuple[str, str, str]:
        if len(line) > self.limits.max_request_line:
            raise UriTooLong(f"request line exceeds {self.limits.max_request_line} bytes")

        parts = line.split(b" ")
        if len(parts) != 3 or not all(parts):
            raise MalformedMessage("request line must be: method SP target SP version")
        raw_method, raw_target, raw_version = parts

        if not all(c in TCHAR for c in raw_method):
            raise MalformedMessage("method is not a token")
        method = raw_method.decode("ascii")
        if method not in KNOWN_METHODS:
            raise NotImplementedError_(f"method {method}")

        if not raw_version.startswith(b"HTTP/"):
            raise MalformedMessage("unrecognised version")
        if raw_version not in (b"HTTP/1.1", b"HTTP/1.0"):
            raise VersionNotSupported(raw_version.decode("ascii", "replace"))
        version = raw_version.decode("ascii")

        # origin-form only. absolute-form is for proxies and we are not one.
        if not raw_target.startswith(b"/"):
            raise MalformedMessage("target must be origin-form (start with /)")
        if any(c < 0x21 or c > 0x7E for c in raw_target):
            raise MalformedMessage("control or non-ASCII byte in target")
        target = raw_target.decode("ascii")

        return method, target, version

    def _parse_fields(self, lines: list[bytes]) -> Headers:
        items: list[tuple[str, str]] = []
        for line in lines:
            if not line:
                continue
            if line[0:1] in (b" ", b"\t"):
                # obs-fold. Deleted from HTTP by RFC 9112 5.2; rejecting it is
                # the modern requirement for anything but a message/http body.
                raise MalformedMessage("obsolete line folding in header block")
            if len(items) >= self.limits.max_header_count:
                raise HeaderFieldsTooLarge(f"more than {self.limits.max_header_count} fields")

            raw_name, sep, raw_value = line.partition(b":")
            if not sep:
                raise MalformedMessage("header field without a colon")
            if not raw_name or not all(c in TCHAR for c in raw_name):
                # Catches "Name : value" too: the trailing space is not a tchar.
                raise MalformedMessage("header field name is not a token")

            value = raw_value.strip(b" \t")
            if any(c < 0x20 and c != 0x09 for c in value):
                raise MalformedMessage("control character in header value")

            items.append(
                (raw_name.decode("ascii"), value.decode("latin-1"))
            )
        return Headers(items)

    def _decide_framing(self, headers: Headers) -> tuple[str, int]:
        """Answer the only question that matters: how long is the body?"""
        has_te = "transfer-encoding" in headers
        has_cl = "content-length" in headers

        if has_te and has_cl:
            # The classic smuggling pair: two answers to one question.
            raise MalformedMessage("both Transfer-Encoding and Content-Length")

        if has_te:
            codings = [
                token.strip().lower()
                for value in headers.get_all("transfer-encoding")
                for token in value.split(",")
                if token.strip()
            ]
            if codings != ["chunked"]:
                raise NotImplementedError_(f"transfer coding {','.join(codings)!r}")
            return "chunked", -1

        if has_cl:
            values = headers.get_all("content-length")
            if len(values) > 1:
                raise MalformedMessage("multiple Content-Length headers")
            raw = values[0]
            if not raw or not raw.isdigit() or not raw.isascii():
                # No sign, no spaces, no hex, no "5, 5".
                raise MalformedMessage(f"Content-Length is not a bare decimal: {raw!r}")
            length = int(raw)
            if length > self.limits.max_body:
                raise PayloadTooLarge(f"body of {length} bytes exceeds {self.limits.max_body}")
            return "length", length

        return "none", 0

    # -- body -------------------------------------------------------------

    def _take_body(self, head: _Head) -> bytes | None:
        if head.framing == "chunked":
            return self._take_chunked_body(head)

        need = head.content_length
        if need == 0:
            return b""
        if len(self._buf) < need:
            return None
        # Exactly `need` bytes. The whole assignment is on this line.
        body = bytes(self._buf[:need])
        del self._buf[:need]
        return body

    def _take_chunked_body(self, head: _Head) -> bytes | None:
        """Decode a chunked body if all of it has arrived.

        Re-scans from the start of the body on every call rather than keeping
        a resumable cursor. That is O(body) work per recv, bounded by
        ``max_body`` -- cheap at this scale, and it keeps the decoder a plain
        function that a reader can check by eye.
        """
        buf = self._buf
        pos = 0
        out = bytearray()

        while True:
            line_end = buf.find(CRLF, pos)
            if line_end == -1:
                if len(buf) - pos > self.limits.max_chunk_line:
                    raise MalformedMessage("chunk size line too long")
                return None
            size_line = bytes(buf[pos:line_end])
            size_field = size_line.split(b";", 1)[0].strip()
            if not size_field or not _is_hex(size_field):
                raise MalformedMessage(f"bad chunk size {size_line!r}")
            size = int(size_field, 16)
            pos = line_end + 2

            if size == 0:
                trailers, consumed = self._take_trailers(buf, pos)
                if consumed is None:
                    return None
                head.trailers = trailers
                del self._buf[: consumed]
                return bytes(out)

            if len(out) + size > self.limits.max_body:
                raise PayloadTooLarge(f"chunked body exceeds {self.limits.max_body} bytes")
            if len(buf) < pos + size + 2:
                return None
            if bytes(buf[pos + size : pos + size + 2]) != CRLF:
                raise MalformedMessage("chunk not terminated by CRLF")
            out += buf[pos : pos + size]
            pos += size + 2

    def _take_trailers(self, buf: bytearray, pos: int) -> tuple[Headers, int | None]:
        items: list[bytes] = []
        while True:
            line_end = buf.find(CRLF, pos)
            if line_end == -1:
                return Headers(), None
            if line_end == pos:  # empty line: end of trailer section
                return self._parse_fields(items), line_end + 2
            items.append(bytes(buf[pos:line_end]))
            if len(items) > self.limits.max_header_count:
                raise HeaderFieldsTooLarge("too many trailer fields")
            pos = line_end + 2


def _is_hex(data: bytes) -> bool:
    return all(c in b"0123456789abcdefABCDEF" for c in data)


def _split_target(target: str) -> tuple[str, str]:
    path, _, query = target.partition("?")
    # Percent-decoding belongs to the path, not to the query: decoding the
    # query here would make an encoded %26 indistinguishable from a real &.
    return unquote(path), query
