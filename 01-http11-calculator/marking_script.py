from __future__ import annotations

import socket
import sys

REQUESTS = [
    ("GET", "/add?a=2&b=3", 200),
    ("GET", "/sub?a=10&b=4", 200),
    ("GET", "/mul?a=6&b=7", 200),
    ("GET", "/div?a=1&b=0", 400),
    ("GET", "/pow?a=2&b=8", 404),
    ("POST", "/add", 405),
]


def read_response(sock: socket.socket, buf: bytearray) -> tuple[int, dict[str, str], bytes]:
    while (index := buf.find(b"\r\n\r\n")) == -1:
        data = sock.recv(65536)
        if not data:
            raise ConnectionError("server closed the connection mid-response")
        buf += data
    head = bytes(buf[:index]).decode("latin-1")
    del buf[: index + 4]

    status_line, *fields = head.split("\r\n")
    status = int(status_line.split(" ")[1])
    headers = {}
    for line in fields:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()

    length = int(headers.get("content-length", 0))
    while len(buf) < length:
        data = sock.recv(65536)
        if not data:
            raise ConnectionError("server closed the connection mid-body")
        buf += data
    body = bytes(buf[:length])
    del buf[:length]
    return status, headers, body


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    sock = socket.create_connection(("localhost", port), timeout=5)
    buf = bytearray()
    failures = 0

    for method, target, expected in REQUESTS:
        sock.sendall(f"{method} {target} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
        status, headers, body = read_response(sock, buf)
        ok = status == expected
        failures += not ok
        shown = body.decode("utf-8", "replace") if status == 200 else ""
        print(f"{'ok  ' if ok else 'FAIL'} {method:4} {target:18} -> {status}  {shown}")

    sock.setblocking(False)
    try:
        still_open = bool(sock.recv(1, socket.MSG_PEEK))
    except BlockingIOError:
        still_open = True
    except OSError:
        still_open = False
    sock.setblocking(True)

    print()
    print(f"socket still open: {still_open}")
    print(f"1 TCP handshake, {len(REQUESTS)} responses")
    print(f"leftover unread bytes: {len(buf)}")

    sock.close()
    return 1 if failures or not still_open or buf else 0


if __name__ == "__main__":
    raise SystemExit(main())
