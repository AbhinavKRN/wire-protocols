"""A static file server is a program that hands out files on request from
strangers. These are the requests it must not honour.

Every hostile frame here is assembled by hand rather than with
``encode_request``, because our own encoder refuses to build most of them --
and an attacker is not using our encoder.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from bhttp.frame import FLAG_END_MESSAGE, Frame, FrameType
from bhttp.messages import (
    Request,
    decode_error,
    decode_response_head,
    encode_request,
)
from support import RawPeer, ServerFixture, unhex


def hostile_request(path: str, *, stream_id: int = 1, method: int = 0x01) -> Frame:
    """A REQUEST frame built straight from bytes, validator bypassed."""
    encoded = path.encode("latin-1")
    payload = bytes([method]) + len(encoded).to_bytes(2, "big") + encoded + b"\x00"
    return Frame(
        type=FrameType.REQUEST, flags=FLAG_END_MESSAGE, stream_id=stream_id, payload=payload
    )


class PathTraversalTests(unittest.TestCase):
    """Each of these is an attempt to name a file outside the document root."""

    def assert_rejected(self, path: str, status: int = 400) -> None:
        with ServerFixture() as server, RawPeer(server.address) as peer:
            peer.send_frames([hostile_request(path)])
            frames = peer.read_message_frames()
            response = decode_response_head(frames[0].payload, 1)
            self.assertEqual(response.status, status, f"{path!r} should be {status}")
            # Stream-level: the answer is a refusal, not a disconnection.
            self.assertTrue(peer.is_open(), "a bad path must not cost the connection")

            # And the connection still works afterwards.
            peer.send_frames(encode_request(Request(path="/hello.txt"), 3))
            self.assertEqual(
                decode_response_head(peer.read_message_frames()[0].payload, 3).status, 200
            )

    def test_dot_dot(self):
        self.assert_rejected("/../SPEC.md")

    def test_deep_dot_dot(self):
        self.assert_rejected("/../../../../../../etc/passwd")

    def test_dot_dot_in_the_middle(self):
        self.assert_rejected("/docs/../../SPEC.md")

    def test_percent_encoded_dot_dot(self):
        self.assert_rejected("/%2e%2e/SPEC.md")

    def test_double_encoded_dot_dot(self):
        # %252e decodes to %2e, which is *not* decoded again. One pass only.
        self.assert_rejected("/%252e%252e/SPEC.md", status=404)

    def test_single_dot_segment(self):
        self.assert_rejected("/./hello.txt")

    def test_backslash_separator(self):
        self.assert_rejected("/..\\SPEC.md")

    def test_windows_drive_letter(self):
        self.assert_rejected("/C:/Windows/win.ini")

    def test_ntfs_alternate_data_stream(self):
        self.assert_rejected("/hello.txt:$DATA")

    def test_windows_device_name(self):
        self.assert_rejected("/nul")

    def test_device_name_with_extension(self):
        self.assert_rejected("/con.txt")

    def test_percent_encoded_nul(self):
        self.assert_rejected("/hello%00.txt")

    def test_absolute_unix_path_is_just_a_name(self):
        """A leading slash is the root of the document root, not of the disk."""
        self.assert_rejected("//etc/passwd", status=404)

    def test_a_long_path_is_refused_before_it_is_resolved(self):
        self.assert_rejected("/" + "a" * 9000)


class SymlinkTests(unittest.TestCase):
    def test_a_symlink_out_of_the_root_is_not_followed(self):
        with tempfile.TemporaryDirectory() as outside, tempfile.TemporaryDirectory() as root:
            secret = Path(outside) / "secret.txt"
            secret.write_text("this must not be served")
            link = Path(root) / "escape.txt"
            try:
                os.symlink(secret, link)
            except (OSError, NotImplementedError) as exc:
                raise unittest.SkipTest(f"cannot create symlinks here: {exc}") from exc

            with ServerFixture(root) as server, RawPeer(server.address) as peer:
                peer.send_frames([hostile_request("/escape.txt")])
                response = decode_response_head(peer.read_message_frames()[0].payload, 1)
                self.assertEqual(response.status, 400)
                self.assertNotIn(b"must not be served", response.body)


class FrameLevelAttackTests(unittest.TestCase):
    """Failures where frame boundaries are in doubt: answer, then hang up."""

    def test_oversized_frame_length_is_refused_without_buffering(self):
        with ServerFixture() as server, RawPeer(server.address) as peer:
            # Length 0x010000 = 65536, one over MAX_FRAME_SIZE. Note we never
            # send the payload: the server must reject on the header alone.
            peer.send(unhex("01 00 00 01 01 00 00 01"))
            frame = peer.read_frame()
            self.assertEqual(frame.type, FrameType.ERROR)
            error = decode_error(frame.payload)
            self.assertEqual(error.status, 400)
            self.assertIn("MAX_FRAME_SIZE", error.reason)
            self.assertTrue(peer.wait_until_closed())

    def test_a_peer_that_does_not_speak_the_protocol(self):
        with ServerFixture() as server:
            peer = RawPeer(server.address, preface=False)
            with peer:
                peer.send(b"GET /index.html HTTP/1.1\r\nHost: x\r\n\r\n")
                frame = peer.read_frame()
                error = decode_error(frame.payload)
                self.assertEqual(error.status, 400)
                self.assertIn("preface", error.reason)
                self.assertTrue(peer.wait_until_closed())

    def test_stream_id_reuse_closes_the_connection(self):
        with ServerFixture() as server, RawPeer(server.address) as peer:
            peer.send_frames(encode_request(Request(path="/hello.txt"), 1))
            peer.read_message_frames()
            peer.send_frames(encode_request(Request(path="/hello.txt"), 1))
            frame = peer.read_frame()
            self.assertEqual(frame.type, FrameType.ERROR)
            self.assertTrue(peer.wait_until_closed())

    def test_body_larger_than_the_limit_is_413(self):
        with ServerFixture(max_body=4096) as server, RawPeer(server.address) as peer:
            head, *data = encode_request(
                Request(method="POST", path="/hello.txt", body=b"x" * 20000), 1, max_data=4096
            )
            peer.send_frames([head, *data])
            response = decode_response_head(peer.read_message_frames()[0].payload, 1)
            self.assertEqual(response.status, 413)

    def test_a_truncated_frame_never_produces_a_partial_message(self):
        with ServerFixture() as server, RawPeer(server.address) as peer:
            raw = encode_request(Request(path="/hello.txt"), 1)[0].encode()
            peer.send(raw[:-3])  # three bytes short
            self.assertTrue(peer.is_open(0.4), "the server must simply wait")
            peer.send(raw[-3:])
            self.assertEqual(
                decode_response_head(peer.read_message_frames()[0].payload, 1).status, 200
            )


class DirectoryTests(unittest.TestCase):
    def test_directory_without_an_index_is_404_not_a_listing(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "empty").mkdir()
            (Path(root) / "index.html").write_text("root index")
            with ServerFixture(root) as server, RawPeer(server.address) as peer:
                peer.send_frames([hostile_request("/empty")])
                response = decode_response_head(peer.read_message_frames()[0].payload, 1)
                self.assertEqual(response.status, 404)


if __name__ == "__main__":
    unittest.main()
