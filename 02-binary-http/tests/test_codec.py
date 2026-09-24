from __future__ import annotations

import unittest

from bhttp.errors import ConnectionFailure, StreamFailure
from bhttp.frame import (
    HEADER_SIZE,
    MAX_FRAME_SIZE,
    PREFACE,
    ByteReader,
    Frame,
    FrameReader,
    FrameType,
    StreamUnderflow,
    decode_header,
)
from bhttp.headers import (
    INDEX_BY_NAME,
    STATIC_TABLE,
    HeaderFormatError,
    decode_header_block,
    encode_header_block,
)
from bhttp.messages import (
    MessageAssembler,
    Request,
    Response,
    encode_request,
    encode_response,
    validate_path,
)


class FrameEncodingTests(unittest.TestCase):
    def test_header_is_always_eight_bytes(self):
        for payload in (b"", b"x", bytes(1000)):
            frame = Frame(type=FrameType.DATA, payload=payload)
            self.assertEqual(len(frame.encode()), HEADER_SIZE + len(payload))

    def test_round_trips_every_field(self):
        frame = Frame(type=0x7F, flags=0xA5, stream_id=0xBEEF, payload=b"hi", reserved=0x5A)
        length, type_, flags, reserved, stream_id = decode_header(frame.encode()[:HEADER_SIZE])
        self.assertEqual((length, type_, flags, reserved, stream_id), (2, 0x7F, 0xA5, 0x5A, 0xBEEF))

    def test_refuses_to_encode_an_oversized_payload(self):
        with self.assertRaises(ValueError):
            Frame(type=FrameType.DATA, payload=bytes(MAX_FRAME_SIZE + 1)).encode()

    def test_refuses_a_stream_id_that_does_not_fit(self):
        with self.assertRaises(ValueError):
            Frame(type=FrameType.DATA, stream_id=0x10000).encode()

    def test_maximum_sized_frame_is_allowed(self):
        frame = Frame(type=FrameType.DATA, payload=bytes(MAX_FRAME_SIZE))
        reader = FrameReader()
        reader.feed(frame.encode())
        self.assertEqual(len(reader.next_frame().payload), MAX_FRAME_SIZE)


class FrameReaderTests(unittest.TestCase):
    def test_consumes_exactly_one_frame(self):
        first = Frame(type=FrameType.DATA, payload=b"aaa").encode()
        second = Frame(type=FrameType.DATA, payload=b"bb").encode()
        reader = FrameReader()
        reader.feed(first + second)
        self.assertEqual(reader.next_frame().payload, b"aaa")
        self.assertEqual(reader.buffered, len(second))
        self.assertEqual(reader.next_frame().payload, b"bb")
        self.assertIsNone(reader.next_frame())

    def test_one_byte_at_a_time(self):
        raw = Frame(type=FrameType.DATA, stream_id=9, payload=b"payload").encode()
        reader = FrameReader()
        for index, byte in enumerate(raw):
            reader.feed(bytes([byte]))
            frame = reader.next_frame()
            if index < len(raw) - 1:
                self.assertIsNone(frame, f"completed early at byte {index}")
        self.assertEqual(frame.payload, b"payload")

    def test_preface_is_required_when_expected(self):
        reader = FrameReader(expect_preface=True)
        reader.feed(PREFACE + Frame(type=FrameType.DATA, payload=b"x").encode())
        self.assertEqual(reader.next_frame().payload, b"x")

    def test_bad_preface_is_caught_on_the_first_wrong_byte(self):
        reader = FrameReader(expect_preface=True)
        reader.feed(b"G")
        with self.assertRaises(ConnectionFailure):
            reader.next_frame()

    def test_partial_but_still_plausible_preface_waits(self):
        reader = FrameReader(expect_preface=True)
        reader.feed(b"BH")
        self.assertIsNone(reader.next_frame())

    def test_oversized_length_is_refused_before_the_payload(self):
        reader = FrameReader()
        reader.feed(bytes([0x01, 0x00, 0x00, 0x03, 0x00, 0x00, 0x00, 0x01]))
        with self.assertRaises(ConnectionFailure):
            reader.next_frame()

    def test_a_lower_local_limit_can_be_enforced(self):
        reader = FrameReader(max_frame_size=16)
        reader.feed(Frame(type=FrameType.DATA, payload=bytes(17)).encode())
        with self.assertRaises(ConnectionFailure):
            reader.next_frame()


class ByteReaderTests(unittest.TestCase):
    def test_underflow_is_a_single_recognisable_failure(self):
        reader = ByteReader(b"\x01")
        with self.assertRaises(StreamUnderflow):
            reader.u16()

    def test_expect_end_catches_trailing_bytes(self):
        reader = ByteReader(b"\x01\x02")
        reader.u8()
        with self.assertRaises(StreamUnderflow):
            reader.expect_end()


class HeaderTests(unittest.TestCase):
    def test_static_table_has_exactly_ten_names(self):
        self.assertEqual(len(STATIC_TABLE) - 1, 10, "index 0 is reserved for the literal form")
        self.assertEqual(len(INDEX_BY_NAME), 10)

    def test_every_static_name_encodes_to_two_bytes_plus_its_value(self):
        for name in INDEX_BY_NAME:
            encoded = encode_header_block([(name, "v")])
            self.assertEqual(len(encoded), 1 + 1 + 2 + 1, f"{name} should use the index form")

    def test_literal_names_round_trip(self):
        block = encode_header_block([("x-custom", "value")])
        self.assertEqual(block[1], 0x00, "literal form starts with a zero byte")
        self.assertEqual(decode_header_block(ByteReader(block)), [("x-custom", "value")])

    def test_encoder_refuses_an_uppercase_name(self):
        with self.assertRaises(ValueError):
            encode_header_block([("Content-Length", "1")])

    def test_encoder_refuses_a_control_character_in_a_value(self):
        with self.assertRaises(HeaderFormatError):
            encode_header_block([("x", "a\r\nb")])

    def test_more_than_255_headers_cannot_be_expressed(self):
        with self.assertRaises(ValueError):
            encode_header_block([("x", "y")] * 256)

    def test_duplicate_names_are_preserved_in_order(self):
        items = [("accept", "a"), ("accept", "b")]
        self.assertEqual(decode_header_block(ByteReader(encode_header_block(items))), items)


class PathTests(unittest.TestCase):
    def test_accepts_ordinary_paths(self):
        for path in ("/", "/index.html", "/a/b/c.txt", "/a%20b", "/q?x=1&y=2", "/.hidden"):
            with self.subTest(path):
                validate_path(path)

    def test_rejects_the_dangerous_ones(self):
        for path in ("", "x", "/..", "/../x", "/./x", "/a\\b", "/a b", "/a%00b", "/" + "a" * 9000):
            with self.subTest(path), self.assertRaises(ValueError):
                validate_path(path)

    def test_a_dot_inside_a_name_is_fine(self):
        validate_path("/..hidden")
        validate_path("/a..b")


class MessageEncodingTests(unittest.TestCase):
    def test_encoder_rejects_an_unknown_method(self):
        with self.assertRaises(ValueError):
            encode_request(Request(method="FROB", path="/"), 1)

    def test_body_is_split_at_max_data(self):
        frames = encode_request(Request(method="POST", path="/", body=b"x" * 10), 1, max_data=4)
        self.assertEqual([len(f.payload) for f in frames[1:]], [4, 4, 2])
        self.assertTrue(frames[-1].end_message)
        self.assertFalse(any(f.end_message for f in frames[:-1]))

    def test_a_bodyless_message_is_one_frame_with_end_message(self):
        frames = encode_response(Response(status=204), 1)
        self.assertEqual(len(frames), 1)
        self.assertTrue(frames[0].end_message)

    def test_max_data_must_be_sane(self):
        with self.assertRaises(ValueError):
            encode_request(Request(path="/"), 1, max_data=0)


class AssemblerTests(unittest.TestCase):
    def test_content_length_is_checked_against_the_delivered_body(self):
        assembler = MessageAssembler("server")
        head, data = encode_request(
            Request(method="POST", path="/", headers=[("content-length", "99")], body=b"short"), 1
        )
        self.assertIsNone(assembler.accept(head))
        with self.assertRaises(StreamFailure):
            assembler.accept(data)

    def test_head_responses_are_exempt_from_the_content_length_check(self):
        assembler = MessageAssembler("client")
        assembler.open_stream(1, expect_body=False)
        frame = encode_response(Response(headers=[("content-length", "4096")]), 1)[0]
        response = assembler.accept(frame)
        self.assertEqual(response.body, b"")

    def test_role_confusion_is_a_connection_failure(self):
        with self.assertRaises(ConnectionFailure):
            MessageAssembler("server").accept(encode_response(Response(), 1)[0])
        with self.assertRaises(ConnectionFailure):
            MessageAssembler("client").accept(encode_request(Request(path="/"), 1)[0])

    def test_role_must_be_one_of_two(self):
        with self.assertRaises(ValueError):
            MessageAssembler("proxy")


if __name__ == "__main__":
    unittest.main()
