#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


from bhttp.errors import BhttpError, ConnectionFailure  # noqa: E402
from bhttp.frame import MAX_FRAME_SIZE, PREFACE, Frame, FrameReader  # noqa: E402
from bhttp.messages import (  # noqa: E402
    ErrorMessage,
    MessageAssembler,
    Request,
    Response,
    encode_request,
)
from tools.hexdump import annotate, render  # noqa: E402

USER_AGENT = "bcurl/1.0"
DEFAULT_PORT = 9000

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_CLIENT_ERROR = 4
EXIT_SERVER_ERROR = 5
EXIT_CONNECT = 7
EXIT_PROTOCOL = 8


@dataclass
class Target:
    host: str
    port: int
    path: str

    @property
    def authority(self) -> str:
        return f"{self.host}:{self.port}"


def parse_url(raw: str) -> Target:
    text = raw.removeprefix("bht://")
    authority, slash, path = text.partition("/")
    path = f"/{path}" if slash else "/"
    host, colon, port_text = authority.rpartition(":")
    if not colon:
        host, port = authority, DEFAULT_PORT
    else:
        if not port_text.isdigit():
            raise ValueError(f"bad port in {raw!r}")
        host, port = host, int(port_text)
    if not host:
        raise ValueError(f"no host in {raw!r}")
    return Target(host, port, path)


@dataclass
class Exchange:
    stream_id: int
    target: Target
    method: str
    response: Response | None = None


@dataclass
class Result:
    exchanges: list[Exchange] = field(default_factory=list)
    errors: list[ErrorMessage] = field(default_factory=list)
    frames_sent: int = 0
    frames_received: int = 0
    connections: int = 0


class Bcurl:
    def __init__(
        self,
        *,
        verbose: bool = False,
        timeout: float = 10.0,
        max_data: int = MAX_FRAME_SIZE,
        extra_headers: list[tuple[str, str]] | None = None,
        trace: object = None,
    ):
        self.verbose = verbose
        self.timeout = timeout
        self.max_data = max_data
        self.extra_headers = extra_headers or []
        self.trace = trace or sys.stderr

    def fetch(self, targets: list[Target], *, method: str = "GET") -> Result:
        result = Result()
        authority = targets[0].authority
        host, port = targets[0].host, targets[0].port

        sock = socket.create_connection((host, port), timeout=self.timeout)
        result.connections = 1
        sock.settimeout(self.timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._note(f"* connected to {authority}")

        assembler = MessageAssembler("client")
        reader = FrameReader()
        outgoing = bytearray(PREFACE)
        self._dump_out(PREFACE, preface=True)

        for index, target in enumerate(targets):
            stream_id = 1 + 2 * index
            request = Request(
                method=method,
                path=target.path,
                headers=[
                    ("host", target.authority),
                    ("user-agent", USER_AGENT),
                    *self.extra_headers,
                ],
            )
            frames = encode_request(request, stream_id, max_data=self.max_data)
            for frame in frames:
                raw = frame.encode()
                outgoing += raw
                self._dump_out(raw)
            result.frames_sent += len(frames)
            assembler.open_stream(stream_id, expect_body=method != "HEAD")
            result.exchanges.append(Exchange(stream_id, target, method))

        try:
            sock.sendall(bytes(outgoing))
            pending = {exchange.stream_id for exchange in result.exchanges}
            by_stream = {exchange.stream_id: exchange for exchange in result.exchanges}

            while pending:
                frame = reader.next_frame()
                if frame is None:
                    data = sock.recv(65536)
                    if not data:
                        raise ConnectionFailure(
                            f"connection closed with {len(pending)} response(s) outstanding"
                        )
                    reader.feed(data)
                    continue

                result.frames_received += 1
                self._dump_in(frame)
                message = assembler.accept(frame)
                if message is None:
                    continue
                if isinstance(message, ErrorMessage):
                    result.errors.append(message)
                    break
                by_stream[message.stream_id].response = message
                pending.discard(message.stream_id)
        finally:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            sock.close()

        return result

    def _note(self, text: str) -> None:
        if self.verbose:
            print(text, file=self.trace)

    def _dump_out(self, raw: bytes, *, preface: bool = False) -> None:
        if not self.verbose:
            return
        dumped = render(annotate(raw, preface=preface))
        print("\n".join(f"> {line}" for line in dumped.splitlines()), file=self.trace)

    def _dump_in(self, frame: Frame) -> None:
        if not self.verbose:
            return
        dumped = render(annotate(frame.encode()))
        print("\n".join(f"< {line}" for line in dumped.splitlines()), file=self.trace)


def exit_code_for(result: Result) -> int:
    if result.errors:
        return EXIT_PROTOCOL
    worst = EXIT_OK
    for exchange in result.exchanges:
        if exchange.response is None:
            return EXIT_PROTOCOL
        if exchange.response.status >= 500:
            worst = max(worst, EXIT_SERVER_ERROR)
        elif exchange.response.status >= 400:
            worst = max(worst, EXIT_CLIENT_ERROR)
    return worst


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bcurl", description="a BHT/1 client")
    parser.add_argument("url", nargs="+", help="[bht://]host[:port]/path")
    parser.add_argument("-v", "--verbose", action="store_true", help="hexdump every frame")
    parser.add_argument("-I", "--head", action="store_true", help="send HEAD instead of GET")
    parser.add_argument("-o", "--output", help="write bodies to this file instead of stdout")
    parser.add_argument("-H", "--header", action="append", default=[], help="name:value")
    parser.add_argument("--max-data", type=int, default=MAX_FRAME_SIZE)
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args(argv)

    try:
        targets = [parse_url(url) for url in args.url]
    except ValueError as exc:
        print(f"bcurl: {exc}", file=sys.stderr)
        return EXIT_USAGE

    authorities = {target.authority for target in targets}
    if len(authorities) > 1:
        print(
            f"bcurl: all URLs must share one host:port, got {sorted(authorities)}",
            file=sys.stderr,
        )
        return EXIT_USAGE

    extra_headers = []
    for item in args.header:
        name, sep, value = item.partition(":")
        if not sep:
            print(f"bcurl: bad header {item!r}, expected name:value", file=sys.stderr)
            return EXIT_USAGE
        extra_headers.append((name.strip().lower(), value.strip()))

    client = Bcurl(
        verbose=args.verbose,
        timeout=args.timeout,
        max_data=args.max_data,
        extra_headers=extra_headers,
    )

    try:
        result = client.fetch(targets, method="HEAD" if args.head else "GET")
    except OSError as exc:
        print(f"bcurl: cannot connect to {targets[0].authority}: {exc}", file=sys.stderr)
        return EXIT_CONNECT
    except BhttpError as exc:
        print(f"bcurl: protocol error: {exc.reason}", file=sys.stderr)
        return EXIT_PROTOCOL

    for error in result.errors:
        print(f"bcurl: server sent ERROR {error.status}: {error.reason}", file=sys.stderr)

    with contextlib.ExitStack() as stack:
        sink = (
            stack.enter_context(open(args.output, "wb")) if args.output else sys.stdout.buffer
        )
        for exchange in result.exchanges:
            response = exchange.response
            if response is None:
                continue
            if args.verbose or response.status >= 400:
                print(
                    f"* {exchange.method} {exchange.target.path} -> {response.status} "
                    f"({len(response.body)} bytes)",
                    file=sys.stderr,
                )
            sink.write(response.body)
        sink.flush()

    if args.verbose:
        print(
            f"* {result.connections} TCP connection, {result.frames_sent} frames out, "
            f"{result.frames_received} frames in",
            file=sys.stderr,
        )
    return exit_code_for(result)


if __name__ == "__main__":
    raise SystemExit(main())
