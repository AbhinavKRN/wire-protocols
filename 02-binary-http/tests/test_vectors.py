from __future__ import annotations

import json
import unittest
from pathlib import Path

from bhttp.errors import ConnectionFailure, StreamFailure
from bhttp.frame import PREFACE, FrameReader
from bhttp.messages import (
    ErrorMessage,
    MessageAssembler,
    Request,
    Response,
    encode_request,
    encode_response,
)

VECTOR_DIR = Path(__file__).parent / "vectors"


def unhex(text: str) -> bytes:
    return bytes.fromhex(text.replace(" ", "").replace("\n", ""))


def load(name: str) -> list[dict]:
    path = VECTOR_DIR / name
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def drive(data: bytes, vector: dict, *, chunk: int = 0):
    role = vector["role"]
    wants_preface = role == "server" and vector.get("preface", True)
    reader = FrameReader(expect_preface=wants_preface)
    assembler = MessageAssembler(role)
    for stream_id in vector.get("open_streams", []):
        assembler.open_stream(stream_id)

    wire = (PREFACE if wants_preface else b"") + data
    messages = []

    def pump() -> None:
        while (frame := reader.next_frame()) is not None:
            message = assembler.accept(frame)
            if message is not None:
                messages.append(message)

    if chunk:
        for index in range(0, len(wire), chunk):
            reader.feed(wire[index : index + chunk])
            pump()
    else:
        reader.feed(wire)
        pump()
    return messages, assembler


class ValidVectorTests(unittest.TestCase):
    def check(self, vector: dict, *, chunk: int = 0) -> None:
        messages, assembler = drive(unhex(vector["hex"]), vector, chunk=chunk)
        expected = vector["expect"]
        self.assertEqual(
            len(messages), len(expected), f"{len(expected)} message(s) expected from these bytes"
        )

        for message, want in zip(messages, expected, strict=True):
            self.assertEqual(message.stream_id, want["stream_id"])
            if want["kind"] == "request":
                self.assertIsInstance(message, Request)
                self.assertEqual(message.method, want["method"])
                self.assertEqual(message.path, want["path"])
                self.assertEqual([list(h) for h in message.headers], want["headers"])
                self.assertEqual(message.body, want["body"].encode("latin-1"))
            elif want["kind"] == "response":
                self.assertIsInstance(message, Response)
                self.assertEqual(message.status, want["status"])
                self.assertEqual([list(h) for h in message.headers], want["headers"])
                self.assertEqual(message.body, want["body"].encode("latin-1"))
            else:
                self.assertIsInstance(message, ErrorMessage)
                self.assertEqual(message.status, want["status"])
                self.assertEqual(message.reason, want["reason"])

        self.assertEqual(assembler.skipped_unknown, vector.get("skipped_unknown", 0))

    def test_request_vectors(self):
        for vector in load("requests.jsonl"):
            with self.subTest(vector["name"]):
                self.check(vector)

    def test_response_vectors(self):
        for vector in load("responses.jsonl"):
            with self.subTest(vector["name"]):
                self.check(vector)

    def test_every_vector_survives_one_byte_at_a_time(self):
        for name in ("requests.jsonl", "responses.jsonl"):
            for vector in load(name):
                with self.subTest(f"{name}: {vector['name']}"):
                    self.check(vector, chunk=1)

    def test_canonical_vectors_are_reproduced_byte_for_byte(self):
        for name in ("requests.jsonl", "responses.jsonl"):
            for vector in load(name):
                if not vector.get("canonical"):
                    continue
                with self.subTest(f"{name}: {vector['name']}"):
                    want = vector["expect"][0]
                    kwargs = {}
                    if "max_data" in vector:
                        kwargs["max_data"] = vector["max_data"]
                    if want["kind"] == "request":
                        frames = encode_request(
                            Request(
                                method=want["method"],
                                path=want["path"],
                                headers=[tuple(h) for h in want["headers"]],
                                body=want["body"].encode("latin-1"),
                            ),
                            want["stream_id"],
                            **kwargs,
                        )
                    else:
                        frames = encode_response(
                            Response(
                                status=want["status"],
                                headers=[tuple(h) for h in want["headers"]],
                                body=want["body"].encode("latin-1"),
                            ),
                            want["stream_id"],
                            **kwargs,
                        )
                    produced = b"".join(frame.encode() for frame in frames)
                    self.assertEqual(produced.hex(" "), unhex(vector["hex"]).hex(" "))


class InvalidVectorTests(unittest.TestCase):

    def test_invalid_vectors_are_rejected(self):
        for vector in load("invalid.jsonl"):
            with self.subTest(vector["name"]):
                expected = ConnectionFailure if vector["reject"] == "connection" else StreamFailure
                with self.assertRaises(expected) as caught:
                    drive(unhex(vector["hex"]), vector)
                self.assertEqual(caught.exception.status, vector["status"])

    def test_a_stream_failure_leaves_the_connection_usable(self):
        bad = unhex("00 00 05 01 01 00 00 01  00 00 01 2f 00")
        good = unhex("00 00 09 01 01 00 00 03  01 00 01 2f 01 83 00 01 78")

        reader = FrameReader(expect_preface=True)
        assembler = MessageAssembler("server")
        reader.feed(PREFACE + bad + good)

        with self.assertRaises(StreamFailure):
            assembler.accept(reader.next_frame())

        message = assembler.accept(reader.next_frame())
        self.assertIsInstance(message, Request)
        self.assertEqual(message.stream_id, 3)


if __name__ == "__main__":
    unittest.main()
