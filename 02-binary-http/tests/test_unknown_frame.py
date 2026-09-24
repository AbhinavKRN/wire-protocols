"""SPEC 3.1: a receiver meeting a frame type it does not know MUST skip it
cleanly.

This is the line the brief says may not be skipped, so it gets its own file.
An extensibility clause that is written down but never exercised is a wish;
the only thing that makes it a mechanism is a test that sends a frame from
the future and watches the connection carry on.
"""

from __future__ import annotations

import contextlib
import socket
import threading
import unittest

from bcurl import Bcurl, Target
from bhttp.frame import FLAG_END_MESSAGE, PREFACE, Frame, FrameType
from bhttp.messages import (
    MessageAssembler,
    Request,
    Response,
    decode_response_head,
    encode_request,
    encode_response,
)
from support import RawPeer, ServerFixture

FUTURE_TYPE = 0x7F  # unassigned in v1; imagine v2 uses it for SETTINGS


def future_frame(payload: bytes = b"\xde\xad\xbe\xef", stream_id: int = 0) -> Frame:
    return Frame(type=FUTURE_TYPE, flags=0x04, stream_id=stream_id, payload=payload)


class ServerSkipsUnknownFrames(unittest.TestCase):
    def test_unknown_frame_between_two_requests(self):
        with ServerFixture() as server, RawPeer(server.address) as peer:
            peer.send_frames(encode_request(Request(path="/hello.txt"), 1))
            self.assertEqual(peer.read_message_frames()[0].stream_id, 1)

            peer.send_frames([future_frame()])
            peer.send_frames(encode_request(Request(path="/hello.txt"), 3))
            frames = peer.read_message_frames()

            self.assertEqual(frames[0].stream_id, 3, "the frame after the unknown one")
            self.assertEqual(decode_response_head(frames[0].payload, 3).status, 200)
            self.assertTrue(peer.is_open(), "an unknown frame must not cost the connection")

            stats = server.stats.snapshot()
            self.assertEqual(stats["accepted"], 1)
            self.assertEqual(stats["skipped_unknown"], 1)
            self.assertEqual(stats["responses"], 2, "the unknown frame got no reply")

    def test_unknown_frame_with_a_large_payload_is_stepped_over_exactly(self):
        """The skip has to consume the payload precisely; one byte out and
        the next frame header is read from the middle of the last one."""
        with ServerFixture() as server, RawPeer(server.address) as peer:
            peer.send_frames([future_frame(payload=bytes(range(256)) * 40)])  # 10240 bytes
            peer.send_frames(encode_request(Request(path="/hello.txt"), 1))
            frames = peer.read_message_frames()
            self.assertEqual(decode_response_head(frames[0].payload, 1).status, 200)
            self.assertEqual(server.stats.snapshot()["skipped_unknown"], 1)

    def test_unknown_frame_arriving_in_the_same_packet(self):
        with ServerFixture() as server, RawPeer(server.address) as peer:
            blob = future_frame().encode() + b"".join(
                frame.encode() for frame in encode_request(Request(path="/hello.txt"), 1)
            )
            peer.send(blob)
            self.assertEqual(
                decode_response_head(peer.read_message_frames()[0].payload, 1).status, 200
            )

    def test_unknown_frame_does_not_disturb_a_message_in_progress(self):
        """Interleaved between a request head and its body."""
        with ServerFixture() as server, RawPeer(server.address) as peer:
            head, data = encode_request(Request(method="POST", path="/hello.txt", body=b"xy"), 1)
            peer.send_frames([head, future_frame(stream_id=1), data])
            frames = peer.read_message_frames()
            # POST on a static file is 405, but it is an *answer*: the body
            # was assembled across the interruption.
            self.assertEqual(decode_response_head(frames[0].payload, 1).status, 405)
            self.assertTrue(peer.is_open())

    def test_unassigned_flag_bits_and_reserved_byte_are_ignored(self):
        """SPEC 2.3. A v2 sender setting a flag we have never heard of must
        not be treated as an error."""
        with ServerFixture() as server, RawPeer(server.address) as peer:
            original = encode_request(Request(path="/hello.txt"), 1)[0]
            polluted = Frame(
                type=original.type,
                flags=original.flags | 0x40,  # unassigned bit
                stream_id=original.stream_id,
                payload=original.payload,
                reserved=0xFF,  # MUST be ignored on receipt
            )
            peer.send_frames([polluted])
            self.assertEqual(
                decode_response_head(peer.read_message_frames()[0].payload, 1).status, 200
            )
            self.assertTrue(peer.is_open())


class FutureServer:
    """A server from a later version of the protocol: it sends frame types
    and flags this client has never heard of, around a perfectly ordinary
    response."""

    def __init__(self, response: Response):
        self.response = response
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.address = self.sock.getsockname()
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> FutureServer:
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        with contextlib.suppress(OSError):
            self.sock.close()

    def _serve(self) -> None:
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(5)
            received = b""
            while len(received) < len(PREFACE) + 8:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                received += chunk

            out = [future_frame(b"settings from the future")]
            out += encode_response(self.response, 1)
            out.append(future_frame(b"trailing chatter", stream_id=1))
            conn.sendall(b"".join(frame.encode() for frame in out))
            with contextlib.suppress(OSError):
                conn.recv(65536)


class ClientSkipsUnknownFrames(unittest.TestCase):
    def test_bcurl_ignores_frames_from_a_later_version(self):
        response = Response(
            status=200,
            headers=[("content-type", "text/plain"), ("content-length", "5")],
            body=b"hello",
        )
        with FutureServer(response) as server:
            result = Bcurl(timeout=5).fetch([Target(server.address[0], server.address[1], "/x")])

        self.assertEqual(result.exchanges[0].response.status, 200)
        self.assertEqual(result.exchanges[0].response.body, b"hello")


class AssemblerSkipTests(unittest.TestCase):
    """The same rule at the unit level, away from any socket."""

    def test_assembler_counts_and_ignores_unknown_types(self):
        assembler = MessageAssembler("server")
        for type_code in (0x05, 0x40, 0x7F, 0xFF):
            frame = Frame(type=type_code, flags=FLAG_END_MESSAGE, stream_id=0, payload=b"?")
            self.assertIsNone(assembler.accept(frame))
        self.assertEqual(assembler.skipped_unknown, 4)

        head = encode_request(Request(path="/"), 1)[0]
        message = assembler.accept(head)
        self.assertIsInstance(message, Request)

    def test_known_types_are_exactly_the_four_in_the_spec(self):
        self.assertEqual(
            sorted(int(member) for member in FrameType), [0x01, 0x02, 0x03, 0x04]
        )
        self.assertFalse(FrameType.is_known(0x05))
        self.assertFalse(FrameType.is_known(0xFF))


if __name__ == "__main__":
    unittest.main()
