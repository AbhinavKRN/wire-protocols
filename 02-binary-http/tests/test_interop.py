"""bserve and bcurl against each other, over real sockets.

These tests are the weaker half of the conformance story -- two halves of one
codebase agreeing with each other proves less than the vectors do. What they
do prove is that the socket plumbing, the stream bookkeeping and the exit
codes behave, and that the connection is genuinely reused.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bcurl import EXIT_CLIENT_ERROR, EXIT_OK, Bcurl, Target, exit_code_for, main, parse_url
from bhttp.frame import FrameType
from bhttp.messages import Request, encode_request
from support import WWW, RawPeer, ServerFixture


class UrlTests(unittest.TestCase):
    def test_parses_the_forms_the_brief_uses(self):
        self.assertEqual(
            parse_url("localhost:9000/index.html"), Target("localhost", 9000, "/index.html")
        )
        self.assertEqual(parse_url("bht://localhost:9000/a/b"), Target("localhost", 9000, "/a/b"))
        self.assertEqual(parse_url("localhost:9000"), Target("localhost", 9000, "/"))
        self.assertEqual(parse_url("example.org/x"), Target("example.org", 9000, "/x"))

    def test_rejects_a_bad_port(self):
        with self.assertRaises(ValueError):
            parse_url("localhost:http/x")


class FetchTests(unittest.TestCase):
    def test_fetches_a_file(self):
        with ServerFixture() as server:
            result = Bcurl().fetch([Target(*server.address, "/index.html")])
            response = result.exchanges[0].response
            self.assertEqual(response.status, 200)
            self.assertEqual(response.body, (WWW / "index.html").read_bytes())
            self.assertEqual(response.header("content-type"), "text/html")
            self.assertEqual(response.header("content-length"), str(len(response.body)))
            self.assertEqual(server.stats.snapshot()["accepted"], 1)

    def test_directory_serves_its_index(self):
        with ServerFixture() as server:
            result = Bcurl().fetch([Target(*server.address, "/")])
            self.assertEqual(result.exchanges[0].response.body, (WWW / "index.html").read_bytes())

    def test_missing_file_is_404_and_exit_code_4(self):
        with ServerFixture() as server:
            result = Bcurl().fetch([Target(*server.address, "/nope.txt")])
            self.assertEqual(result.exchanges[0].response.status, 404)
            self.assertEqual(exit_code_for(result), EXIT_CLIENT_ERROR)

    def test_head_has_the_length_but_no_body(self):
        with ServerFixture() as server:
            result = Bcurl().fetch([Target(*server.address, "/hello.txt")], method="HEAD")
            response = result.exchanges[0].response
            self.assertEqual(response.status, 200)
            self.assertEqual(response.body, b"")
            self.assertEqual(
                response.header("content-length"), str(len((WWW / "hello.txt").read_bytes()))
            )
            self.assertEqual(result.frames_received, 1)

    def test_three_urls_share_one_connection(self):
        """`and never open a second connection`."""
        with ServerFixture() as server:
            targets = [
                Target(*server.address, "/index.html"),
                Target(*server.address, "/hello.txt"),
                Target(*server.address, "/docs/nested.txt"),
            ]
            result = Bcurl().fetch(targets)

            self.assertEqual(result.connections, 1)
            self.assertEqual(server.stats.snapshot()["accepted"], 1)
            self.assertEqual([e.stream_id for e in result.exchanges], [1, 3, 5])
            self.assertEqual(
                [e.response.body for e in result.exchanges],
                [
                    (WWW / "index.html").read_bytes(),
                    (WWW / "hello.txt").read_bytes(),
                    (WWW / "docs" / "nested.txt").read_bytes(),
                ],
            )

    def test_a_large_body_arrives_in_several_data_frames(self):
        with tempfile.TemporaryDirectory() as root:
            payload = bytes(range(256)) * 900
            (Path(root) / "big.bin").write_bytes(payload)
            with ServerFixture(root) as server:
                peer = RawPeer(server.address)
                with peer:
                    peer.send_frames(
                        encode_request(Request(path="/big.bin"), 1)
                    )
                    frames = peer.read_message_frames()
                    self.assertEqual(frames[0].type, FrameType.RESPONSE)
                    self.assertGreater(len(frames), 4, "body must be split across DATA frames")
                    self.assertTrue(all(f.type == FrameType.DATA for f in frames[1:]))
                    self.assertEqual(peer.body_of(frames), payload)
                    self.assertTrue(peer.is_open())

    def test_the_connection_survives_many_sequential_requests(self):
        with ServerFixture() as server, RawPeer(server.address) as peer:
            for index in range(20):
                stream_id = 1 + 2 * index
                peer.send_frames(encode_request(Request(path="/hello.txt"), stream_id))
                frames = peer.read_message_frames()
                self.assertEqual(frames[0].stream_id, stream_id)
            self.assertEqual(server.stats.snapshot()["accepted"], 1)
            self.assertTrue(peer.is_open())

    def test_post_to_a_static_file_is_405(self):
        with ServerFixture() as server, RawPeer(server.address) as peer:
            peer.send_frames(encode_request(Request(method="POST", path="/hello.txt"), 1))
            frames = peer.read_message_frames()
            from bhttp.messages import decode_response_head

            response = decode_response_head(frames[0].payload, 1)
            self.assertEqual(response.status, 405)
            self.assertTrue(peer.is_open(), "405 is a stream-level answer, not a hang-up")


class CommandLineTests(unittest.TestCase):
    def test_main_writes_the_body_and_returns_zero(self):
        with ServerFixture() as server, tempfile.TemporaryDirectory() as out:
            path = Path(out) / "body"
            host, port = server.address
            code = main(["-o", str(path), f"{host}:{port}/hello.txt"])
            self.assertEqual(code, EXIT_OK)
            self.assertEqual(path.read_bytes(), (WWW / "hello.txt").read_bytes())

    def test_main_returns_4_on_a_404(self):
        with ServerFixture() as server, tempfile.TemporaryDirectory() as out:
            host, port = server.address
            code = main(["-o", str(Path(out) / "body"), f"{host}:{port}/missing"])
            self.assertEqual(code, EXIT_CLIENT_ERROR)

    def test_main_refuses_two_authorities(self):
        """Because honouring it would mean opening a second connection."""
        self.assertEqual(main(["localhost:9000/a", "localhost:9001/b"]), 2)

    def test_verbose_hexdump_names_every_field(self):
        import io

        with ServerFixture() as server:
            trace = io.StringIO()
            client = Bcurl(verbose=True, trace=trace)
            client.fetch([Target(*server.address, "/hello.txt")])
            dump = trace.getvalue()

        for expected in [
            'Connection preface "BHT1"',
            "Type = 0x01 REQUEST",
            "Flags = 0x01 END_MESSAGE",
            "Reserved, MUST be ignored on receipt",
            "Stream ID = 1",
            'Path = "/hello.txt"',
            'Static index 3 -> "host"',
            "Status = 200",
            "Body bytes",
        ]:
            self.assertIn(expected, dump)


class ResponseShapeTests(unittest.TestCase):
    def test_response_headers_all_come_from_the_static_table(self):
        """Every name bserve sends is one of the ten, so no response head
        contains a literal name. That is the whole point of numbering them."""
        with ServerFixture() as server:
            result = Bcurl().fetch([Target(*server.address, "/index.html")])
            head = result.exchanges[0].response
            self.assertEqual(
                [name for name, _ in head.headers],
                ["content-type", "content-length", "last-modified", "date", "server"],
            )

        from bhttp.headers import INDEX_BY_NAME

        for name, _ in head.headers:
            self.assertIn(name, INDEX_BY_NAME)


if __name__ == "__main__":
    unittest.main()
