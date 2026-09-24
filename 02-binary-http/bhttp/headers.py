from __future__ import annotations

from .frame import ByteReader

STATIC_TABLE: tuple[str, ...] = (
    "",
    "content-length",
    "content-type",
    "host",
    "date",
    "server",
    "connection",
    "user-agent",
    "accept",
    "last-modified",
    "cache-control",
)

INDEX_BY_NAME = {name: index for index, name in enumerate(STATIC_TABLE) if index}

LITERAL_PREFIX = 0x00
INDEX_FLAG = 0x80

TOKEN_CHARS = frozenset("!#$%&'*+-.^_`|~0123456789abcdefghijklmnopqrstuvwxyz")


class HeaderFormatError(ValueError):
    pass


def encode_header_block(items: list[tuple[str, str]]) -> bytes:
    if len(items) > 255:
        raise ValueError(f"{len(items)} headers exceeds the 255 a u8 count can express")
    out = bytearray([len(items)])
    for name, value in items:
        out += encode_entry(name, value)
    return bytes(out)


def encode_entry(name: str, value: str) -> bytes:
    lowered = name.lower()
    if lowered != name:
        raise ValueError(f"header name must be lowercase on the wire: {name!r}")
    _check_name(lowered)
    _check_value(value)

    out = bytearray()
    index = INDEX_BY_NAME.get(lowered)
    if index is not None:
        out.append(INDEX_FLAG | index)
    else:
        encoded_name = lowered.encode("ascii")
        if len(encoded_name) > 255:
            raise ValueError("literal header name longer than 255 bytes")
        out.append(LITERAL_PREFIX)
        out.append(len(encoded_name))
        out += encoded_name

    encoded_value = value.encode("latin-1")
    if len(encoded_value) > 0xFFFF:
        raise ValueError("header value longer than 65535 bytes")
    out.append((len(encoded_value) >> 8) & 0xFF)
    out.append(len(encoded_value) & 0xFF)
    out += encoded_value
    return bytes(out)


def decode_header_block(reader: ByteReader) -> list[tuple[str, str]]:
    count = reader.u8()
    items: list[tuple[str, str]] = []
    for _ in range(count):
        items.append(decode_entry(reader))
    return items


def decode_entry(reader: ByteReader) -> tuple[str, str]:
    prefix = reader.u8()
    if prefix & INDEX_FLAG:
        index = prefix & 0x7F
        if index == 0 or index >= len(STATIC_TABLE):
            raise HeaderFormatError(f"static table index {index} does not exist")
        name = STATIC_TABLE[index]
    elif prefix == LITERAL_PREFIX:
        length = reader.u8()
        if length == 0:
            raise HeaderFormatError("literal header name is empty")
        name = reader.take(length).decode("latin-1")
        _check_name(name)
    else:
        raise HeaderFormatError(f"unassigned header entry prefix 0x{prefix:02x}")

    value = reader.take(reader.u16()).decode("latin-1")
    _check_value(value)
    return name, value


def _check_name(name: str) -> None:
    if not name:
        raise HeaderFormatError("header name is empty")
    if any(char not in TOKEN_CHARS for char in name):
        raise HeaderFormatError(f"header name is not a lowercase token: {name!r}")


def _check_value(value: str) -> None:
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise HeaderFormatError("control character in header value")


def get(items: list[tuple[str, str]], name: str, default: str | None = None) -> str | None:
    for candidate, value in items:
        if candidate == name:
            return value
    return default
