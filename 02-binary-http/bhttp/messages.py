from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from urllib.parse import unquote

from .errors import ConnectionFailure, StreamFailure
from .frame import (
    FLAG_END_MESSAGE,
    MAX_FRAME_SIZE,
    ByteReader,
    Frame,
    FrameType,
    StreamUnderflow,
)
from .headers import decode_header_block, encode_header_block, get

MAX_PATH = 8192
DEFAULT_MAX_BODY = 8 * 1024 * 1024


class Method(IntEnum):
    GET = 0x01
    HEAD = 0x02
    POST = 0x03
    PUT = 0x04
    DELETE = 0x05
    OPTIONS = 0x06
    PATCH = 0x07


METHOD_BY_NAME = {member.name: int(member) for member in Method}
NAME_BY_METHOD = {int(member): member.name for member in Method}


@dataclass
class Request:
    method: str = "GET"
    path: str = "/"
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: bytes = b""
    stream_id: int = 0

    def header(self, name: str, default: str | None = None) -> str | None:
        return get(self.headers, name, default)


@dataclass
class Response:
    status: int = 200
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: bytes = b""
    stream_id: int = 0

    def header(self, name: str, default: str | None = None) -> str | None:
        return get(self.headers, name, default)


@dataclass
class ErrorMessage:

    status: int
    reason: str
    stream_id: int = 0


def encode_request(request: Request, stream_id: int, *, max_data: int = MAX_FRAME_SIZE):
    validate_path(request.path)
    if request.method not in METHOD_BY_NAME:
        raise ValueError(f"unknown method {request.method!r}")

    encoded_path = request.path.encode("ascii")
    payload = bytearray([METHOD_BY_NAME[request.method]])
    payload += len(encoded_path).to_bytes(2, "big")
    payload += encoded_path
    payload += encode_header_block(request.headers)
    return _frames(FrameType.REQUEST, bytes(payload), request.body, stream_id, max_data)


def encode_response(response: Response, stream_id: int, *, max_data: int = MAX_FRAME_SIZE):
    payload = bytearray(response.status.to_bytes(2, "big"))
    payload += encode_header_block(response.headers)
    return _frames(FrameType.RESPONSE, bytes(payload), response.body, stream_id, max_data)


def encode_error(status: int, reason: str, stream_id: int = 0) -> Frame:
    encoded = reason.encode("utf-8")[:0xFFFF]
    payload = status.to_bytes(2, "big") + len(encoded).to_bytes(2, "big") + encoded
    return Frame(
        type=FrameType.ERROR, flags=FLAG_END_MESSAGE, stream_id=stream_id, payload=payload
    )


def _frames(
    head_type: FrameType, head_payload: bytes, body: bytes, stream_id: int, max_data: int
) -> list[Frame]:
    if len(head_payload) > MAX_FRAME_SIZE:
        raise ValueError(
            f"head payload of {len(head_payload)} bytes exceeds MAX_FRAME_SIZE {MAX_FRAME_SIZE}"
        )
    if not 0 < max_data <= MAX_FRAME_SIZE:
        raise ValueError(f"max_data must be in 1..{MAX_FRAME_SIZE}")

    chunks = [body[i : i + max_data] for i in range(0, len(body), max_data)]
    frames = [Frame(type=head_type, stream_id=stream_id, payload=head_payload)]
    frames += [Frame(type=FrameType.DATA, stream_id=stream_id, payload=chunk) for chunk in chunks]
    last = frames[-1]
    frames[-1] = Frame(
        type=last.type,
        flags=last.flags | FLAG_END_MESSAGE,
        stream_id=last.stream_id,
        payload=last.payload,
    )
    return frames


def decode_request_head(payload: bytes, stream_id: int) -> Request:
    reader = ByteReader(payload, stream_id=stream_id)
    try:
        code = reader.u8()
        if code not in NAME_BY_METHOD:
            raise StreamFailure(
                f"unassigned method code 0x{code:02x}", status=400, stream_id=stream_id
            )
        path = reader.take(reader.u16()).decode("ascii", "strict")
        headers = decode_header_block(reader)
        reader.expect_end()
    except (StreamUnderflow, ValueError) as exc:
        raise StreamFailure(str(exc), status=400, stream_id=stream_id) from exc

    try:
        validate_path(path)
    except ValueError as exc:
        raise StreamFailure(str(exc), status=400, stream_id=stream_id) from exc

    return Request(method=NAME_BY_METHOD[code], path=path, headers=headers, stream_id=stream_id)


def decode_response_head(payload: bytes, stream_id: int) -> Response:
    reader = ByteReader(payload, stream_id=stream_id)
    try:
        status = reader.u16()
        headers = decode_header_block(reader)
        reader.expect_end()
    except (StreamUnderflow, ValueError) as exc:
        raise StreamFailure(str(exc), status=400, stream_id=stream_id) from exc
    return Response(status=status, headers=headers, stream_id=stream_id)


def decode_error(payload: bytes, stream_id: int = 0) -> ErrorMessage:
    reader = ByteReader(payload)
    try:
        status = reader.u16()
        reason = reader.take(reader.u16()).decode("utf-8", "replace")
    except (StreamUnderflow, ValueError) as exc:
        raise ConnectionFailure(f"malformed ERROR frame: {exc}") from exc
    return ErrorMessage(status=status, reason=reason, stream_id=stream_id)


def validate_path(path: str) -> None:
    if not path:
        raise ValueError("path is empty")
    if not path.startswith("/"):
        raise ValueError(f"path must start with '/': {path!r}")
    if len(path) > MAX_PATH:
        raise ValueError(f"path exceeds {MAX_PATH} bytes")
    for char in path:
        if not 0x21 <= ord(char) <= 0x7E:
            raise ValueError(f"byte 0x{ord(char):02x} is not allowed in a path")

    decoded = unquote(path.split("?", 1)[0])
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in decoded):
        raise ValueError("control character in percent-decoded path")
    if "\\" in decoded:
        raise ValueError("backslash in path")
    if any(segment in (".", "..") for segment in decoded.split("/")):
        raise ValueError("dot segment in path")


@dataclass
class _Pending:
    stream_id: int
    head: Request | Response
    body: bytearray = field(default_factory=bytearray)
    draining: bool = False


class MessageAssembler:

    def __init__(self, role: str, *, max_body: int = DEFAULT_MAX_BODY):
        if role not in ("server", "client"):
            raise ValueError("role must be 'server' or 'client'")
        self.role = role
        self.max_body = max_body
        self.skipped_unknown = 0
        self._pending: dict[int, _Pending] = {}
        self._completed: set[int] = set()
        self._highest_stream = 0
        self._expected: dict[int, bool] = {}

    def open_stream(self, stream_id: int, *, expect_body: bool = True) -> None:
        self._expected[stream_id] = expect_body

    def accept(self, frame: Frame) -> Request | Response | ErrorMessage | None:
        if not frame.known:
            self.skipped_unknown += 1
            return None

        if frame.type == FrameType.ERROR:
            return decode_error(frame.payload, frame.stream_id)
        if frame.type == FrameType.REQUEST:
            return self._accept_head(frame, "request")
        if frame.type == FrameType.RESPONSE:
            return self._accept_head(frame, "response")
        return self._accept_data(frame)

    def _accept_head(self, frame: Frame, kind: str):
        if kind == "request" and self.role != "server":
            raise ConnectionFailure("a client must not receive REQUEST frames")
        if kind == "response" and self.role != "client":
            raise ConnectionFailure("a server must not receive RESPONSE frames")

        stream_id = frame.stream_id
        if kind == "request":
            self._check_new_stream(stream_id)
            self._highest_stream = stream_id
        else:
            if stream_id not in self._expected:
                raise ConnectionFailure(f"response on stream {stream_id}, which was never opened")
            if stream_id in self._completed:
                raise ConnectionFailure(f"second response on stream {stream_id}")
        if stream_id in self._pending:
            raise ConnectionFailure(f"stream {stream_id} already has a message in flight")

        try:
            head = (
                decode_request_head(frame.payload, stream_id)
                if kind == "request"
                else decode_response_head(frame.payload, stream_id)
            )
        except StreamFailure:
            if not frame.end_message:
                self._pending[stream_id] = _Pending(
                    stream_id, Request(stream_id=stream_id), draining=True
                )
            else:
                self._completed.add(stream_id)
            raise

        pending = _Pending(stream_id, head)
        if frame.end_message:
            return self._finish(pending)
        self._pending[stream_id] = pending
        return None

    def _check_new_stream(self, stream_id: int) -> None:
        if stream_id == 0:
            raise ConnectionFailure("stream 0 is reserved for the connection")
        if stream_id % 2 == 0:
            raise ConnectionFailure(f"even stream id {stream_id} is reserved for server push")
        if stream_id <= self._highest_stream:
            raise ConnectionFailure(
                f"stream id {stream_id} is not greater than {self._highest_stream}"
            )

    def _accept_data(self, frame: Frame):
        pending = self._pending.get(frame.stream_id)
        if pending is None:
            raise ConnectionFailure(
                f"DATA on stream {frame.stream_id}, which is not open"
            )
        if len(pending.body) + len(frame.payload) > self.max_body:
            del self._pending[frame.stream_id]
            self._completed.add(frame.stream_id)
            raise StreamFailure(
                f"body exceeds {self.max_body} bytes", status=413, stream_id=frame.stream_id
            )
        pending.body += frame.payload
        if not frame.end_message:
            return None

        del self._pending[frame.stream_id]
        if pending.draining:
            self._completed.add(frame.stream_id)
            return None
        return self._finish(pending)

    def _finish(self, pending: _Pending):
        self._pending.pop(pending.stream_id, None)
        self._completed.add(pending.stream_id)
        message = pending.head
        message.body = bytes(pending.body)

        declared = get(message.headers, "content-length")
        checkable = declared is not None and self._expected.get(pending.stream_id, True)
        if checkable and (not declared.isdigit() or int(declared) != len(message.body)):
            raise StreamFailure(
                f"content-length {declared!r} disagrees with {len(message.body)} body bytes",
                status=400,
                stream_id=pending.stream_id,
            )
        return message
