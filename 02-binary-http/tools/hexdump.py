"""Annotated hexdump of BHT/1 bytes.

One renderer, two consumers: `bcurl -v` prints it live, and HEXDUMP.md is
generated from it. The deliverable says that if you cannot annotate your own
bytes, the spec is not finished -- so the annotator is written against the
spec's field list, and any field it cannot name is a field the spec did not
define properly.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bhttp.frame import (  # noqa: E402
    HEADER_SIZE,
    PREFACE,
    ByteReader,
    FrameType,
    decode_header,
)
from bhttp.headers import STATIC_TABLE  # noqa: E402
from bhttp.messages import NAME_BY_METHOD  # noqa: E402

FLAG_NAMES = {0x01: "END_MESSAGE"}


@dataclass
class Annotation:
    offset: int
    data: bytes
    note: str


def annotate(raw: bytes, *, preface: bool = False, base: int = 0) -> list[Annotation]:
    """Annotate a whole byte stream: optional preface, then frames."""
    out: list[Annotation] = []
    pos = 0
    if preface and raw[:4] == PREFACE:
        out.append(Annotation(base, raw[:4], 'Connection preface "BHT1"'))
        pos = 4
    while pos + HEADER_SIZE <= len(raw):
        annotations, consumed = annotate_frame(raw[pos:], base=base + pos)
        out += annotations
        if consumed == 0:
            break
        pos += consumed
    if pos < len(raw):
        out.append(Annotation(base + pos, raw[pos:], "incomplete trailing bytes"))
    return out


def annotate_frame(raw: bytes, *, base: int = 0) -> tuple[list[Annotation], int]:
    if len(raw) < HEADER_SIZE:
        return [Annotation(base, raw, "truncated frame header")], 0

    length, type_, flags, _reserved, stream_id = decode_header(raw[:HEADER_SIZE])
    known = FrameType.is_known(type_)
    type_name = FrameType(type_).name if known else "UNKNOWN (must be skipped)"

    flag_note = ", ".join(name for bit, name in FLAG_NAMES.items() if flags & bit) or "none"
    if flags & ~0x01:
        flag_note += f", unassigned bits 0x{flags & ~0x01:02x} (ignored)"

    out = [
        Annotation(base, raw[0:3], f"Length = {length}"),
        Annotation(base + 3, raw[3:4], f"Type = 0x{type_:02x} {type_name}"),
        Annotation(base + 4, raw[4:5], f"Flags = 0x{flags:02x} {flag_note}"),
        Annotation(base + 5, raw[5:6], "Reserved, MUST be ignored on receipt"),
        Annotation(base + 6, raw[6:8], f"Stream ID = {stream_id}"),
    ]

    payload = raw[HEADER_SIZE : HEADER_SIZE + length]
    if len(payload) < length:
        out.append(Annotation(base + HEADER_SIZE, payload, "truncated payload"))
        return out, 0

    out += _annotate_payload(payload, type_, base + HEADER_SIZE)
    return out, HEADER_SIZE + length


def _annotate_payload(payload: bytes, type_: int, base: int) -> list[Annotation]:
    try:
        if type_ == FrameType.REQUEST:
            return _annotate_request(payload, base)
        if type_ == FrameType.RESPONSE:
            return _annotate_response(payload, base)
        if type_ == FrameType.ERROR:
            return _annotate_error(payload, base)
        if type_ == FrameType.DATA:
            return [Annotation(base, payload, f"Body bytes ({len(payload)})")]
    except Exception as exc:  # noqa: BLE001
        return [Annotation(base, payload, f"undecodable payload: {exc}")]
    return [Annotation(base, payload, "opaque payload of an unknown frame type")]


def _annotate_request(payload: bytes, base: int) -> list[Annotation]:
    reader = ByteReader(payload)
    out = []
    code = reader.u8()
    out.append(
        Annotation(base, payload[0:1], f"Method = 0x{code:02x} {NAME_BY_METHOD.get(code, '?')}")
    )
    start = reader.position
    length = reader.u16()
    out.append(Annotation(base + start, payload[start : start + 2], f"Path length = {length}"))
    start = reader.position
    path = reader.take(length)
    out.append(Annotation(base + start, path, f'Path = "{path.decode("ascii", "replace")}"'))
    out += _annotate_header_block(reader, payload, base)
    return out


def _annotate_response(payload: bytes, base: int) -> list[Annotation]:
    reader = ByteReader(payload)
    status = reader.u16()
    out = [Annotation(base, payload[0:2], f"Status = {status}")]
    out += _annotate_header_block(reader, payload, base)
    return out


def _annotate_error(payload: bytes, base: int) -> list[Annotation]:
    reader = ByteReader(payload)
    status = reader.u16()
    out = [Annotation(base, payload[0:2], f"Status = {status}")]
    start = reader.position
    length = reader.u16()
    out.append(Annotation(base + start, payload[start : start + 2], f"Reason length = {length}"))
    start = reader.position
    reason = reader.take(length)
    out.append(
        Annotation(base + start, reason, f'Reason = "{reason.decode("utf-8", "replace")}"')
    )
    return out


def _annotate_header_block(reader: ByteReader, payload: bytes, base: int) -> list[Annotation]:
    start = reader.position
    count = reader.u8()
    out = [Annotation(base + start, payload[start : start + 1], f"Header count = {count}")]
    for _ in range(count):
        start = reader.position
        prefix = reader.u8()
        if prefix & 0x80:
            index = prefix & 0x7F
            name = STATIC_TABLE[index] if index < len(STATIC_TABLE) else "?"
            out.append(
                Annotation(
                    base + start,
                    payload[start : start + 1],
                    f'Static index {index} -> "{name}"',
                )
            )
        else:
            name_length = reader.u8()
            name = reader.take(name_length).decode("latin-1")
            out.append(
                Annotation(
                    base + start,
                    payload[start : reader.position],
                    f'Literal name ({name_length} bytes) = "{name}"',
                )
            )
        start = reader.position
        value_length = reader.u16()
        out.append(
            Annotation(
                base + start, payload[start : start + 2], f"Value length = {value_length}"
            )
        )
        start = reader.position
        value = reader.take(value_length)
        out.append(
            Annotation(base + start, value, f'Value = "{value.decode("latin-1")}"')
        )
    return out


def render(annotations: list[Annotation], *, max_bytes: int = 16) -> str:
    lines = []
    for item in annotations:
        shown = item.data[:max_bytes]
        hex_text = " ".join(f"{byte:02x}" for byte in shown)
        if len(item.data) > max_bytes:
            hex_text += f" ... (+{len(item.data) - max_bytes})"
        lines.append(f"{item.offset:04x}  {hex_text:<52}  {item.note}")
    return "\n".join(lines)


def classic(data: bytes, *, base: int = 0, width: int = 16) -> str:
    lines = []
    for offset in range(0, len(data), width):
        chunk = data[offset : offset + width]
        hex_text = " ".join(f"{byte:02x}" for byte in chunk)
        text = "".join(chr(byte) if 0x20 <= byte < 0x7F else "." for byte in chunk)
        lines.append(f"{base + offset:04x}  {hex_text:<{width * 3}} |{text}|")
    return "\n".join(lines)


def dump(raw: bytes, *, preface: bool = False, title: str = "") -> str:
    parts = []
    if title:
        parts.append(title)
        parts.append("-" * len(title))
    parts.append(classic(raw))
    parts.append("")
    parts.append(render(annotate(raw, preface=preface)))
    return "\n".join(parts)


if __name__ == "__main__":
    data = sys.stdin.buffer.read()
    print(dump(data, preface=data[:4] == PREFACE))
