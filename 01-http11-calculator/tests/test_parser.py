"""Parser unit tests: every one of these is about message boundaries.

No sockets appear here. That is the payoff of keeping the parser I/O-free --
"what happens if the body arrives one byte at a time" is a loop, not a
network condition you have to arrange.
"""

from __future__ import annotations

import unittest

from calcserver.http.errors import (
    HeaderFieldsTooLarge,
    HttpError,
    MalformedMessage,
    NotImplementedError_,
    PayloadTooLarge,
    UriTooLong,
    VersionNotSupported,
)
from calcserver.http.parser import Limits, RequestParser

GET_ADD = b"GET /add?a=2&b=3 HTTP/1.1\r\nHost: localhost\r\n\r\n"
GET_SUB = b"GET /sub?a=10&b=4 HTTP/1.1\r\nHost: localhost\r\n\r\n"


def parse_all(data: bytes, limits: Limits | None = None):
    parser = RequestParser(limits or Limits())
    parser.feed(data)
    out = []
    while (request := parser.next_request()) is not None:
        out.append(request)
    return out, parser


class RequestLineTests(unittest.TestCase):
    def test_parses_a_plain_get(self):
        (request,), parser = parse_all(GET_ADD)
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.path, "/add")
        self.assertEqual(request.query_string, "a=2&b=3")
        self.assertEqual(request.version, "HTTP/1.1")
        self.assertEqual(request.headers.get("host"), "localhost")
        self.assertEqual(request.body, b"")
        self.assertEqual(parser.buffered, 0)

    def test_header_lookup_is_case_insensitive(self):
        (request,), _ = parse_all(b"GET /add?a=1&b=1 HTTP/1.1\r\nHOST: x\r\n\r\n")
        self.assertEqual(request.headers.get("Host"), "x")

    def test_percent_decodes_the_path_but_not_the_query(self):
        (request,), _ = parse_all(b"GET /a%64d?a=%261&b=2 HTTP/1.1\r\nHost: x\r\n\r\n")
        self.assertEqual(request.path, "/add")
        self.assertEqual(request.query_string, "a=%261&b=2")

    def test_tolerates_leading_blank_lines(self):
        (request,), _ = parse_all(b"\r\n\r\n" + GET_ADD)
        self.assertEqual(request.path, "/add")

    def test_rejects_endless_blank_lines(self):
        parser = RequestParser(Limits(max_leading_blank_lines=2))
        parser.feed(b"\r\n" * 5 + GET_ADD)
        with self.assertRaises(MalformedMessage):
            parser.next_request()

    def test_rejects_absolute_form_target(self):
        with self.assertRaises(MalformedMessage):
            parse_all(b"GET http://x/add HTTP/1.1\r\nHost: x\r\n\r\n")

    def test_rejects_two_spaces_in_request_line(self):
        with self.assertRaises(MalformedMessage):
            parse_all(b"GET  /add HTTP/1.1\r\nHost: x\r\n\r\n")

    def test_unknown_method_is_501(self):
        with self.assertRaises(NotImplementedError_):
            parse_all(b"FROB /add HTTP/1.1\r\nHost: x\r\n\r\n")

    def test_unsupported_version_is_505(self):
        with self.assertRaises(VersionNotSupported):
            parse_all(b"GET /add HTTP/2.0\r\nHost: x\r\n\r\n")

    def test_request_line_length_is_bounded(self):
        target = b"/add?a=" + b"1" * 9000
        with self.assertRaises(UriTooLong):
            parse_all(b"GET " + target + b" HTTP/1.1\r\nHost: x\r\n\r\n", Limits(max_head=1 << 20))


class HeaderBlockTests(unittest.TestCase):
    def test_rejects_bare_lf(self):
        with self.assertRaises(MalformedMessage):
            parse_all(b"GET /add HTTP/1.1\nHost: x\r\n\r\n")

    def test_rejects_obsolete_line_folding(self):
        with self.assertRaises(MalformedMessage):
            parse_all(b"GET /add HTTP/1.1\r\nHost: x\r\n  continued\r\n\r\n")

    def test_rejects_space_before_colon(self):
        with self.assertRaises(MalformedMessage):
            parse_all(b"GET /add HTTP/1.1\r\nHost : x\r\n\r\n")

    def test_rejects_header_without_colon(self):
        with self.assertRaises(MalformedMessage):
            parse_all(b"GET /add HTTP/1.1\r\nHost\r\n\r\n")

    def test_head_size_is_bounded(self):
        padding = b"X-Pad: " + b"." * 4000 + b"\r\n"
        with self.assertRaises(HeaderFieldsTooLarge):
            parse_all(
                b"GET /add HTTP/1.1\r\nHost: x\r\n" + padding * 5 + b"\r\n",
                Limits(max_head=4096),
            )

    def test_header_count_is_bounded(self):
        fields = b"".join(b"X-%d: y\r\n" % i for i in range(20))
        with self.assertRaises(HeaderFieldsTooLarge):
            parse_all(
                b"GET /add HTTP/1.1\r\nHost: x\r\n" + fields + b"\r\n",
                Limits(max_header_count=10),
            )

    def test_incomplete_head_is_not_an_error(self):
        parser = RequestParser()
        parser.feed(b"GET /add HTTP/1.1\r\nHost: loc")
        self.assertIsNone(parser.next_request())


class FramingTests(unittest.TestCase):
    """Where does this request end and the next one begin."""

    def test_consumes_exactly_content_length_bytes(self):
        body = b"0123456789"
        wire = (
            b"POST /add HTTP/1.1\r\nHost: x\r\nContent-Length: 10\r\n\r\n"
            + body
            + b"THIS BELONGS TO THE NEXT REQUEST"
        )
        parser = RequestParser()
        parser.feed(wire)
        request = parser.next_request()
        self.assertEqual(request.body, body)
        self.assertEqual(bytes(parser._buf), b"THIS BELONGS TO THE NEXT REQUEST")

    def test_two_requests_in_one_packet(self):
        requests, parser = parse_all(GET_ADD + GET_SUB)
        self.assertEqual([r.path for r in requests], ["/add", "/sub"])
        self.assertEqual(parser.buffered, 0)

    def test_six_requests_in_one_packet(self):
        wire = GET_ADD * 6
        requests, _ = parse_all(wire)
        self.assertEqual(len(requests), 6)

    def test_one_byte_at_a_time(self):
        """The adversarial case: no chunk boundary coincides with anything."""
        wire = GET_ADD + GET_SUB
        parser = RequestParser()
        seen = []
        for index in range(len(wire)):
            parser.feed(wire[index : index + 1])
            while (request := parser.next_request()) is not None:
                seen.append((request.path, index))

        self.assertEqual([path for path, _ in seen], ["/add", "/sub"])
        self.assertEqual(seen[0][1], len(GET_ADD) - 1)
        self.assertEqual(seen[1][1], len(wire) - 1)

    def test_body_split_across_feeds(self):
        head = b"POST /add HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n\r\n"
        parser = RequestParser()
        parser.feed(head + b"ab")
        self.assertIsNone(parser.next_request())
        parser.feed(b"cd")
        self.assertIsNone(parser.next_request())
        parser.feed(b"e")
        self.assertEqual(parser.next_request().body, b"abcde")

    def test_no_body_framing_means_zero_bytes(self):
        (request,), _ = parse_all(GET_ADD)
        self.assertEqual(request.framing, "none")
        self.assertEqual(request.raw_length, len(GET_ADD))

    def test_rejects_duplicate_content_length(self):
        with self.assertRaises(MalformedMessage) as caught:
            parse_all(
                b"POST /add HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n"
                b"Content-Length: 6\r\n\r\nabcde"
            )
        self.assertTrue(caught.exception.close)

    def test_rejects_content_length_list(self):
        with self.assertRaises(MalformedMessage):
            parse_all(b"POST /add HTTP/1.1\r\nHost: x\r\nContent-Length: 5, 5\r\n\r\nabcde")

    def test_rejects_signed_content_length(self):
        with self.assertRaises(MalformedMessage):
            parse_all(b"POST /add HTTP/1.1\r\nHost: x\r\nContent-Length: +5\r\n\r\nabcde")

    def test_rejects_transfer_encoding_and_content_length_together(self):
        with self.assertRaises(MalformedMessage):
            parse_all(
                b"POST /add HTTP/1.1\r\nHost: x\r\n"
                b"Content-Length: 5\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n"
            )

    def test_oversized_body_is_413(self):
        with self.assertRaises(PayloadTooLarge):
            parse_all(
                b"POST /add HTTP/1.1\r\nHost: x\r\nContent-Length: 999999\r\n\r\n",
                Limits(max_body=1024),
            )

    def test_unknown_transfer_coding_is_501(self):
        with self.assertRaises(NotImplementedError_):
            parse_all(b"POST /add HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: gzip\r\n\r\n")


class DeferredErrorTests(unittest.TestCase):
    """A semantic error must not desynchronise the stream.

    Missing Host is discovered while parsing the head, but the body has not
    been read off the wire yet. Raising immediately would leave those body
    bytes to be parsed as the next request line -- and then every subsequent
    request on the connection is garbage.
    """

    def test_missing_host_is_raised_only_after_the_body_is_consumed(self):
        wire = (
            b"POST /add HTTP/1.1\r\nContent-Length: 11\r\n\r\nhello world" + GET_SUB
        )
        parser = RequestParser()
        parser.feed(wire)

        with self.assertRaises(HttpError) as caught:
            parser.next_request()
        self.assertEqual(caught.exception.status, 400)
        self.assertFalse(caught.exception.close, "a framed message must not cost the connection")

        following = parser.next_request()
        self.assertEqual(following.path, "/sub")

    def test_multiple_hosts_closes_the_connection(self):
        with self.assertRaises(MalformedMessage) as caught:
            parse_all(b"GET /add HTTP/1.1\r\nHost: a\r\nHost: b\r\n\r\n")
        self.assertTrue(caught.exception.close)

    def test_http_10_does_not_require_host(self):
        (request,), _ = parse_all(b"GET /add?a=1&b=2 HTTP/1.0\r\n\r\n")
        self.assertEqual(request.version, "HTTP/1.0")


class ChunkedTests(unittest.TestCase):
    def test_decodes_a_chunked_body(self):
        wire = (
            b"POST /add HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n"
        )
        (request,), parser = parse_all(wire)
        self.assertEqual(request.body, b"hello world")
        self.assertEqual(request.framing, "chunked")
        self.assertEqual(parser.buffered, 0)

    def test_chunked_body_split_across_feeds(self):
        head = b"POST /add HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
        parser = RequestParser()
        for piece in [head, b"5\r\nhel", b"lo\r\n", b"6\r\n wor", b"ld\r\n", b"0\r\n"]:
            parser.feed(piece)
            self.assertIsNone(parser.next_request())
        parser.feed(b"\r\n")
        self.assertEqual(parser.next_request().body, b"hello world")

    def test_chunk_extensions_are_ignored(self):
        wire = (
            b"POST /add HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"5;name=value\r\nhello\r\n0\r\n\r\n"
        )
        (request,), _ = parse_all(wire)
        self.assertEqual(request.body, b"hello")

    def test_trailers_are_captured(self):
        wire = (
            b"POST /add HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"5\r\nhello\r\n0\r\nX-Checksum: 1234\r\n\r\n"
        )
        (request,), parser = parse_all(wire)
        self.assertEqual(request.trailers.get("x-checksum"), "1234")
        self.assertEqual(parser.buffered, 0)

    def test_a_request_after_a_chunked_one_still_parses(self):
        wire = (
            b"POST /add HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"5\r\nhello\r\n0\r\n\r\n" + GET_SUB
        )
        requests, parser = parse_all(wire)
        self.assertEqual([r.path for r in requests], ["/add", "/sub"])
        self.assertEqual(parser.buffered, 0)

    def test_rejects_bad_chunk_size(self):
        wire = (
            b"POST /add HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"zz\r\nhello\r\n0\r\n\r\n"
        )
        with self.assertRaises(MalformedMessage):
            parse_all(wire)

    def test_rejects_chunk_not_terminated_by_crlf(self):
        wire = (
            b"POST /add HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"5\r\nhelloXX0\r\n\r\n"
        )
        with self.assertRaises(MalformedMessage):
            parse_all(wire)

    def test_chunked_body_size_is_bounded(self):
        wire = (
            b"POST /add HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"400\r\n" + b"x" * 1024 + b"\r\n0\r\n\r\n"
        )
        with self.assertRaises(PayloadTooLarge):
            parse_all(wire, Limits(max_body=512))


if __name__ == "__main__":
    unittest.main()
