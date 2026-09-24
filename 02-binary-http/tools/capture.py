#!/usr/bin/env python3
"""Capture one real BHT/1 exchange and write HEXDUMP.md.

    python tools/capture.py

Starts bserve on an ephemeral port, performs one request with a plain socket
while recording every byte in both directions, and renders the result with
the same annotator `bcurl -v` uses. The bytes in HEXDUMP.md are therefore
traffic, not an illustration.
"""

from __future__ import annotations

import socket
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bhttp.frame import PREFACE, FrameReader  # noqa: E402
from bhttp.messages import Request, encode_request  # noqa: E402
from bserve import Bserve, Config  # noqa: E402
from tools.hexdump import annotate, classic, render  # noqa: E402

TARGET = "/hello.txt"


def capture() -> tuple[bytes, bytes]:
    server = Bserve(ROOT / "www", 0, config=Config(idle_timeout=5)).start()
    try:
        request = Request(
            method="GET",
            path=TARGET,
            headers=[("host", f"localhost:{server.port}"), ("user-agent", "bcurl/1.0")],
        )
        sent = PREFACE + b"".join(
            frame.encode() for frame in encode_request(request, 1)
        )

        sock = socket.create_connection(server.address, timeout=5)
        try:
            sock.sendall(sent)
            reader = FrameReader()
            received = bytearray()
            done = False
            while not done:
                data = sock.recv(65536)
                if not data:
                    break
                received += data
                reader.feed(data)
                while (frame := reader.next_frame()) is not None:
                    done = done or frame.end_message
        finally:
            sock.close()
    finally:
        server.shutdown()
    return sent, bytes(received)


def section(title: str, raw: bytes, *, preface: bool) -> str:
    return "\n".join(
        [
            f"## {title}",
            "",
            f"{len(raw)} bytes on the wire.",
            "",
            "### Raw",
            "",
            "```",
            classic(raw),
            "```",
            "",
            "### Annotated",
            "",
            "```",
            render(annotate(raw, preface=preface)),
            "```",
            "",
        ]
    )


def main() -> int:
    sent, received = capture()
    when = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")

    body_bytes = len(received) - 8 - int.from_bytes(received[0:3], "big") - 8
    document = "\n".join(
        [
            "# One BHT/1 exchange, annotated byte by byte",
            "",
            f"Captured from a live `bserve`/client exchange on {when} by",
            "[`tools/capture.py`](tools/capture.py), and rendered by the same annotator",
            "`bcurl -v` uses. Regenerate with `python tools/capture.py`.",
            "",
            f"The exchange is `GET {TARGET}` on stream 1, over one connection that is still",
            "open when the dump ends.",
            "",
            section("Request (client to server)", sent, preface=True),
            section("Response (server to client)", received, preface=False),
            "## What the bytes show",
            "",
            "**The preface is not a frame.** Four bytes, once per connection, before",
            "anything else. A peer that opens with `GET / HTTP/1.1` is rejected on byte one",
            "instead of being misread as a frame header whose length field happens to say",
            "4,670,532.",
            "",
            "**Every frame begins with its length.** Type is the *fourth* byte, not the",
            "first, so a receiver knows how far to jump before it knows what it is jumping",
            "over. That ordering is the whole of the forward-compatibility rule in SPEC 3.1.",
            "",
            "**The reserved byte is on the wire and means nothing.** It is there so that",
            "version 2 has a field to spend without moving anything, and v1 receivers are",
            "required to ignore whatever appears in it.",
            "",
            "**The response head carries five headers and not one literal name.** Every",
            "name bserve sends is in the ten-entry static table, so each costs a single",
            "byte instead of its spelling: `content-type` is `0x82`, `content-length` is",
            "`0x81`. The values are still text because values are where the entropy is.",
            "",
            "**The body is its own frame.** The response head ends, a DATA frame follows",
            "with its own length and the END_MESSAGE flag, and the message is over. No",
            "chunked encoding, no terminator to scan for, no ambiguity about where the next",
            f"message starts -- the {body_bytes}-byte body was framed by the same 8 bytes that",
            "frame everything else.",
            "",
        ]
    )

    out = ROOT / "HEXDUMP.md"
    out.write_text(document, encoding="utf-8")
    print(f"wrote {out} ({len(sent)} bytes out, {len(received)} bytes in)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
