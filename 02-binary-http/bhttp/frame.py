from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from .errors import ConnectionFailure

PREFACE = b"BHT1"
HEADER_SIZE = 8
MAX_FRAME_SIZE = 65535

FLAG_END_MESSAGE = 0x01


class FrameType(IntEnum):
    REQUEST = 0x01
    RESPONSE = 0x02
    DATA = 0x03
    ERROR = 0x04

    @classmethod
    def is_known(cls, value: int) -> bool:
        return value in _KNOWN_TYPES


_KNOWN_TYPES = {int(member) for member in FrameType}


@dataclass(frozen=True)
class Frame:
    type: int
    flags: int = 0
    stream_id: int = 0
    payload: bytes = b""
    reserved: int = 0

    @property
    def end_message(self) -> bool:
        return bool(self.flags & FLAG_END_MESSAGE)

    @property
    def known(self) -> bool:
        return FrameType.is_known(self.type)

    @property
    def type_name(self) -> str:
        return FrameType(self.type).name if self.known else f"UNKNOWN(0x{self.type:02x})"

    def encode(self) -> bytes:
        length = len(self.payload)
        if length > MAX_FRAME_SIZE:
            raise ValueError(f"payload of {length} bytes exceeds MAX_FRAME_SIZE")
        if not 0 <= self.stream_id <= 0xFFFF:
            raise ValueError(f"stream id {self.stream_id} does not fit in 16 bits")
        header = bytes(
            (
                (length >> 16) & 0xFF,
                (length >> 8) & 0xFF,
                length & 0xFF,
                self.type & 0xFF,
                self.flags & 0xFF,
                self.reserved & 0xFF,
                (self.stream_id >> 8) & 0xFF,
                self.stream_id & 0xFF,
            )
        )
        return header + self.payload


def decode_header(data: bytes) -> tuple[int, int, int, int, int]:
    if len(data) < HEADER_SIZE:
        raise ValueError("frame header is 8 bytes")
    length = (data[0] << 16) | (data[1] << 8) | data[2]
    stream_id = (data[6] << 8) | data[7]
    return length, data[3], data[4], data[5], stream_id


class FrameReader:

    def __init__(self, *, expect_preface: bool = False, max_frame_size: int = MAX_FRAME_SIZE):
        self._buf = bytearray()
        self._need_preface = expect_preface
        self.max_frame_size = max_frame_size

    def feed(self, data: bytes) -> None:
        self._buf += data

    @property
    def buffered(self) -> int:
        return len(self._buf)

    @property
    def mid_frame(self) -> bool:
        return bool(self._buf)

    def next_frame(self) -> Frame | None:
        if self._need_preface:
            if len(self._buf) < len(PREFACE):
                if not PREFACE.startswith(bytes(self._buf)):
                    raise ConnectionFailure("connection preface is not BHT1")
                return None
            if bytes(self._buf[: len(PREFACE)]) != PREFACE:
                raise ConnectionFailure("connection preface is not BHT1")
            del self._buf[: len(PREFACE)]
            self._need_preface = False

        if len(self._buf) < HEADER_SIZE:
            return None

        length, type_, flags, reserved, stream_id = decode_header(bytes(self._buf[:HEADER_SIZE]))
        if length > self.max_frame_size:
            raise ConnectionFailure(
                f"frame length {length} exceeds MAX_FRAME_SIZE {self.max_frame_size}"
            )

        total = HEADER_SIZE + length
        if len(self._buf) < total:
            return None

        payload = bytes(self._buf[HEADER_SIZE:total])
        del self._buf[:total]
        return Frame(
            type=type_, flags=flags, stream_id=stream_id, payload=payload, reserved=reserved
        )


class ByteReader:

    def __init__(self, data: bytes, *, stream_id: int = 0):
        self._data = data
        self._pos = 0
        self.stream_id = stream_id

    @property
    def remaining(self) -> int:
        return len(self._data) - self._pos

    @property
    def position(self) -> int:
        return self._pos

    def take(self, count: int) -> bytes:
        if count > self.remaining:
            raise self._underflow(count)
        chunk = self._data[self._pos : self._pos + count]
        self._pos += count
        return chunk

    def u8(self) -> int:
        return self.take(1)[0]

    def u16(self) -> int:
        chunk = self.take(2)
        return (chunk[0] << 8) | chunk[1]

    def expect_end(self) -> None:
        if self.remaining:
            raise StreamUnderflow(
                f"{self.remaining} unexpected trailing byte(s) in payload"
            )

    def _underflow(self, wanted: int) -> Exception:
        return StreamUnderflow(
            f"payload ended mid-field: wanted {wanted} bytes at offset {self._pos}, "
            f"{self.remaining} remain"
        )


class StreamUnderflow(ValueError):
    pass
