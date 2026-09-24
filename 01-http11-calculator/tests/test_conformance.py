from __future__ import annotations

import unittest

from harness import RawClient, ServerFixture


class MarkingScriptTests(unittest.TestCase):
    def test_one_handshake_six_responses_socket_still_open(self):
        with ServerFixture() as server, RawClient(server.address) as client:
            expected = [
                ("GET", "/add?a=2&b=3", 200, "5"),
                ("GET", "/sub?a=10&b=4", 200, "6"),
                ("GET", "/mul?a=6&b=7", 200, "42"),
                ("GET", "/div?a=1&b=0", 400, None),
                ("GET", "/pow?a=2&b=8", 404, None),
                ("POST", "/add", 405, None),
            ]

            for method, target, status, body in expected:
                client.send(client.request(method, target))
                response = client.read_response()
                with self.subTest(target=f"{method} {target}"):
                    self.assertEqual(response.status, status)
                    if body is not None:
                        self.assertEqual(response.text, body)
                    self.assertTrue(
                        response.keep_alive, "every one of these must stay on the line"
                    )

            self.assertTrue(client.is_open(), "socket still open: True")
            self.assertEqual(client.buffered, 0, "server sent bytes we did not account for")

            stats = server.stats.snapshot()
            self.assertEqual(stats["accepted"], 1, "1 TCP handshake")
            self.assertEqual(stats["responses"], 6, "6 responses")

            self.assertEqual(client.get("/add?a=1&b=1").text, "2")
            self.assertEqual(server.stats.snapshot()["accepted"], 1)

    def test_405_advertises_what_is_allowed(self):
        with ServerFixture() as server, RawClient(server.address) as client:
            client.send(client.request("POST", "/add"))
            response = client.read_response()
            self.assertEqual(response.status, 405)
            self.assertEqual(response.headers["allow"], "GET, HEAD")


class FeatureSetTests(unittest.TestCase):

    def test_every_listed_case(self):
        cases = [
            ("GET", "/add?a=2&b=3", 200, "5"),
            ("GET", "/sub?a=10&b=4", 200, "6"),
            ("GET", "/mul?a=6&b=7", 200, "42"),
            ("GET", "/div?a=9&b=3", 200, "3"),
            ("GET", "/div?a=1&b=0", 400, None),
            ("GET", "/add?a=x&b=3", 400, None),
            ("GET", "/pow?a=2&b=8", 404, None),
            ("POST", "/add", 405, None),
        ]
        with ServerFixture() as server, RawClient(server.address) as client:
            for method, target, status, body in cases:
                client.send(client.request(method, target))
                response = client.read_response()
                with self.subTest(target=f"{method} {target}"):
                    self.assertEqual(response.status, status)
                    if body is not None:
                        self.assertEqual(response.text, body)
            self.assertEqual(server.stats.snapshot()["accepted"], 1)

    def test_request_without_host_is_400_but_keeps_the_connection(self):
        with ServerFixture() as server, RawClient(server.address) as client:
            client.send(b"GET /add?a=1&b=2 HTTP/1.1\r\n\r\n")
            response = client.read_response()
            self.assertEqual(response.status, 400)
            self.assertTrue(response.keep_alive)
            self.assertEqual(client.get("/add?a=2&b=2").text, "4")
            self.assertEqual(server.stats.snapshot()["accepted"], 1)

    def test_negative_operands_and_exact_division(self):
        with ServerFixture() as server, RawClient(server.address) as client:
            self.assertEqual(client.get("/add?a=-5&b=3").text, "-2")
            self.assertEqual(client.get("/div?a=-9&b=3").text, "-3")
            self.assertEqual(client.get("/div?a=7&b=2").text, "3.5")
            self.assertEqual(client.get("/mul?a=0&b=999").text, "0")
            self.assertEqual(server.stats.snapshot()["accepted"], 1)

    def test_parameter_validation(self):
        with ServerFixture() as server, RawClient(server.address) as client:
            for target in [
                "/add?a=1",
                "/add",
                "/add?a=1&b=2&c=3",
                "/add?a=1&a=2&b=3",
                "/add?a=1.5&b=2",
                "/add?a=&b=2",
                "/add?a",
            ]:
                with self.subTest(target=target):
                    self.assertEqual(client.get(target).status, 400)
            self.assertEqual(server.stats.snapshot()["accepted"], 1)

    def test_head_returns_headers_without_a_body(self):
        with ServerFixture() as server, RawClient(server.address) as client:
            client.send(client.request("HEAD", "/add?a=2&b=3"))
            response = client.read_response(has_body=False)
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["content-length"], "1", "length of the absent body")
            self.assertEqual(response.body, b"")
            self.assertEqual(client.get("/add?a=2&b=3").text, "5")


if __name__ == "__main__":
    unittest.main()
