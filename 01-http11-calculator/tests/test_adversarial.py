from __future__ import annotations

import threading
import unittest

from harness import RawClient, ServerFixture

SIX_REQUESTS = [
    b"GET /add?a=2&b=3 HTTP/1.1\r\nHost: localhost\r\n\r\n",
    b"GET /sub?a=10&b=4 HTTP/1.1\r\nHost: localhost\r\n\r\n",
    b"GET /mul?a=6&b=7 HTTP/1.1\r\nHost: localhost\r\n\r\n",
    b"GET /div?a=1&b=0 HTTP/1.1\r\nHost: localhost\r\n\r\n",
    b"GET /pow?a=2&b=8 HTTP/1.1\r\nHost: localhost\r\n\r\n",
    b"POST /add HTTP/1.1\r\nHost: localhost\r\n\r\n",
]
EXPECTED_STATUSES = [200, 200, 200, 400, 404, 405]


class DeliveryShapeTests(unittest.TestCase):

    def test_one_byte_at_a_time(self):
        with ServerFixture() as server, RawClient(server.address) as client:
            for request in SIX_REQUESTS:
                client.send_slowly(request, chunk=1)
                self.assertEqual(client.read_response().status, EXPECTED_STATUSES[0])
                break
            client.send_slowly(b"".join(SIX_REQUESTS[1:]), chunk=1)
            statuses = [r.status for r in client.read_responses(5)]
            self.assertEqual(statuses, EXPECTED_STATUSES[1:])
            self.assertEqual(server.stats.snapshot()["accepted"], 1)

    def test_two_requests_in_one_packet(self):
        with ServerFixture() as server, RawClient(server.address) as client:
            client.send(SIX_REQUESTS[0] + SIX_REQUESTS[1])
            first, second = client.read_responses(2)
            self.assertEqual((first.status, first.text), (200, "5"))
            self.assertEqual((second.status, second.text), (200, "6"))
            self.assertEqual(server.stats.snapshot()["accepted"], 1)

    def test_pipelining_all_six_in_one_write(self):
        with ServerFixture() as server, RawClient(server.address) as client:
            client.send(b"".join(SIX_REQUESTS))
            responses = client.read_responses(6)
            self.assertEqual([r.status for r in responses], EXPECTED_STATUSES)
            self.assertEqual([responses[i].text for i in range(3)], ["5", "6", "42"])
            self.assertTrue(client.is_open())
            self.assertEqual(client.buffered, 0)

            stats = server.stats.snapshot()
            self.assertEqual(stats, {"accepted": 1, "responses": 6, "active": 1})

    def test_request_split_across_the_header_terminator(self):
        request = SIX_REQUESTS[0]
        for cut in (len(request) - 3, len(request) - 2, len(request) - 1):
            with (
                self.subTest(cut=cut),
                ServerFixture() as server,
                RawClient(server.address) as client,
            ):
                client.send(request[:cut])
                self.assertTrue(client.is_open(0.15))
                client.send(request[cut:])
                self.assertEqual(client.read_response().text, "5")


class BodyFramingTests(unittest.TestCase):
    def test_a_body_is_not_a_request(self):
        smuggled = b"GET /mul?a=6&b=7 HTTP/1.1\r\nHost: localhost\r\n\r\n"
        wire = (
            b"POST /add HTTP/1.1\r\nHost: localhost\r\n"
            b"Content-Length: %d\r\n\r\n" % len(smuggled)
        ) + smuggled

        with ServerFixture() as server, RawClient(server.address) as client:
            client.send(wire)
            first = client.read_response()
            self.assertEqual(first.status, 405, "the POST itself")

            client.send(SIX_REQUESTS[0])
            second = client.read_response()
            self.assertEqual(second.text, "5", "not 42: the body was never a request")
            self.assertEqual(server.stats.snapshot()["responses"], 2)

    def test_chunked_body_is_consumed_and_the_stream_stays_aligned(self):
        wire = (
            b"POST /add HTTP/1.1\r\nHost: localhost\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n"
        )
        with ServerFixture() as server, RawClient(server.address) as client:
            client.send(wire)
            self.assertEqual(client.read_response().status, 405)
            self.assertEqual(client.get("/add?a=2&b=3").text, "5")
            self.assertEqual(server.stats.snapshot()["accepted"], 1)


class ConnectionLifetimeTests(unittest.TestCase):
    def test_connection_close_is_honoured(self):
        with ServerFixture() as server, RawClient(server.address) as client:
            client.send(
                b"GET /add?a=2&b=3 HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
            )
            response = client.read_response()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["connection"], "close")
            self.assertTrue(client.wait_until_closed(2.0), "server promised close and must close")
            self.assertEqual(server.stats.snapshot()["responses"], 1)

    def test_http_10_defaults_to_closing(self):
        with ServerFixture() as server, RawClient(server.address) as client:
            client.send(b"GET /add?a=2&b=3 HTTP/1.0\r\n\r\n")
            response = client.read_response()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["connection"], "close")
            self.assertTrue(client.wait_until_closed(2.0))

    def test_http_10_with_keep_alive_stays_open(self):
        with ServerFixture() as server, RawClient(server.address) as client:
            client.send(b"GET /add?a=2&b=3 HTTP/1.0\r\nConnection: keep-alive\r\n\r\n")
            self.assertEqual(client.read_response().headers["connection"], "keep-alive")
            client.send(b"GET /sub?a=9&b=4 HTTP/1.0\r\nConnection: keep-alive\r\n\r\n")
            self.assertEqual(client.read_response().text, "5")
            self.assertEqual(server.stats.snapshot()["accepted"], 1)

    def test_idle_connection_is_reaped(self):
        with ServerFixture(idle_timeout=0.4) as server, RawClient(server.address) as client:
            self.assertEqual(client.get("/add?a=1&b=1").text, "2")
            self.assertTrue(client.wait_until_closed(3.0))
            self.assertEqual(server.stats.snapshot()["responses"], 1)

    def test_half_sent_request_gets_408(self):
        with ServerFixture(idle_timeout=0.4) as server, RawClient(server.address) as client:
            client.send(b"GET /add?a=1&b=1 HTTP/1.1\r\nHost: localhost\r\n")
            response = client.read_response()
            self.assertEqual(response.status, 408)
            self.assertEqual(response.headers["connection"], "close")
            self.assertTrue(client.wait_until_closed(2.0))
            self.assertEqual(server.stats.snapshot()["accepted"], 1)

    def test_requests_per_connection_are_capped(self):
        with (
            ServerFixture(max_requests_per_connection=3) as server,
            RawClient(server.address) as client,
        ):
            client.send(SIX_REQUESTS[0] * 3)
            responses = client.read_responses(3)
            self.assertEqual([r.keep_alive for r in responses], [True, True, False])
            self.assertTrue(client.wait_until_closed(2.0))
            self.assertEqual(server.stats.snapshot()["responses"], 3)


class HostileInputTests(unittest.TestCase):

    def assert_answered_then_closed(self, wire: bytes, status: int, **fixture_kwargs):
        with ServerFixture(**fixture_kwargs) as server, RawClient(server.address) as client:
            client.send(wire)
            response = client.read_response()
            self.assertEqual(response.status, status)
            self.assertEqual(response.headers["connection"], "close")
            self.assertTrue(client.wait_until_closed(2.0))
            self.assertEqual(server.stats.snapshot()["responses"], 1)

    def test_duplicate_content_length(self):
        self.assert_answered_then_closed(
            b"POST /add HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n"
            b"Content-Length: 6\r\n\r\nabcde",
            400,
        )

    def test_content_length_and_transfer_encoding(self):
        self.assert_answered_then_closed(
            b"POST /add HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
            400,
        )

    def test_bare_lf_line_endings(self):
        self.assert_answered_then_closed(b"GET /add?a=1&b=1 HTTP/1.1\nHost: x\n\n", 400)

    def test_two_host_headers(self):
        self.assert_answered_then_closed(
            b"GET /add?a=1&b=1 HTTP/1.1\r\nHost: a\r\nHost: b\r\n\r\n", 400
        )

    def test_garbage_is_not_mistaken_for_http(self):
        self.assert_answered_then_closed(b"\x16\x03\x01\x02\x00 not http at all\r\n\r\n", 400)

    def test_unknown_method_is_501(self):
        self.assert_answered_then_closed(b"FROB /add HTTP/1.1\r\nHost: x\r\n\r\n", 501)

    def test_unsupported_version_is_505(self):
        self.assert_answered_then_closed(b"GET /add HTTP/3.0\r\nHost: x\r\n\r\n", 505)

    def test_oversized_header_block_is_431(self):
        wire = (
            b"GET /add?a=1&b=1 HTTP/1.1\r\nHost: x\r\n"
            + b"".join(b"X-Pad-%d: %s\r\n" % (i, b"." * 900) for i in range(40))
            + b"\r\n"
        )
        self.assert_answered_then_closed(wire, 431)

    def test_oversized_body_is_413(self):
        self.assert_answered_then_closed(
            b"POST /add HTTP/1.1\r\nHost: x\r\nContent-Length: 99999999\r\n\r\n", 413
        )

    def test_a_dead_connection_does_not_kill_the_server(self):
        with ServerFixture() as server:
            victim = RawClient(server.address)
            victim.send(b"GET /add?a=1&b=1 HTTP/1.1\r\nHost: x\r\n")
            victim.sock.close()

            with RawClient(server.address) as client:
                self.assertEqual(client.get("/add?a=2&b=3").text, "5")
            self.assertEqual(server.stats.snapshot()["accepted"], 2)


class ConcurrencyTests(unittest.TestCase):
    def test_many_connections_at_once(self):
        count = 12
        errors: list[BaseException] = []
        barrier = threading.Barrier(count)

        def worker(n: int) -> None:
            try:
                with RawClient(server.address) as client:
                    barrier.wait(5)
                    for _ in range(5):
                        assert client.get(f"/mul?a={n}&b=3").text == str(n * 3)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        with ServerFixture() as server:
            threads = [threading.Thread(target=worker, args=(n,)) for n in range(count)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(15)

            self.assertEqual(errors, [])
            stats = server.stats.snapshot()
            self.assertEqual(stats["accepted"], count)
            self.assertEqual(stats["responses"], count * 5)


if __name__ == "__main__":
    unittest.main()
