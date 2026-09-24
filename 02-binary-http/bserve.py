#!/usr/bin/env python3
"""bserve -- a BHT/1 static file server.

    ./bserve ./www 9000

Accepts a TCP connection, reads binary request frames, maps the path to a
file under a root, replies with status, headers and the bytes, and keeps the
connection open.
"""

from __future__ import annotations

import argparse
import logging
import mimetypes
import os
import socket
import sys
import threading
from dataclasses import dataclass
from email.utils import formatdate
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent))

import contextlib

from bhttp.errors import ConnectionFailure, StreamFailure  # noqa: E402
from bhttp.frame import MAX_FRAME_SIZE, FrameReader  # noqa: E402
from bhttp.messages import (  # noqa: E402
    DEFAULT_MAX_BODY,
    MessageAssembler,
    Request,
    Response,
    encode_error,
    encode_response,
)

SERVER_TOKEN = "bserve/1.0"
ALLOWED_METHODS = ("GET", "HEAD")
DEFAULT_INDEX = "index.html"

# Names that are devices rather than files on Windows, whatever the extension.
WINDOWS_DEVICE_NAMES = frozenset(
    ["con", "prn", "aux", "nul"]
    + [f"com{n}" for n in range(1, 10)]
    + [f"lpt{n}" for n in range(1, 10)]
)

log = logging.getLogger("bserve")


class StaticFiles:
    """Maps a request path to a file, or refuses to.

    This class is the security boundary of the whole program. Everything it
    does is a way of saying: the peer supplies a *name*, never a path.
    """

    def __init__(self, root: Path, *, index: str = DEFAULT_INDEX):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise NotADirectoryError(self.root)
        self.index = index

    def resolve(self, path: str, stream_id: int = 0) -> Path:
        decoded = unquote(path.split("?", 1)[0])
        segments = [segment for segment in decoded.split("/") if segment]

        for segment in segments:
            # The frame decoder already rejected dot segments, backslashes and
            # control characters (SPEC 5.1). These are the host filesystem's
            # own escape hatches, which the protocol knows nothing about.
            if ":" in segment:
                # "C:/secrets" would make pathlib discard the root entirely,
                # and "file.txt:stream" is an NTFS alternate data stream.
                raise StreamFailure("drive or stream marker in path", stream_id=stream_id)
            if segment.split(".")[0].lower() in WINDOWS_DEVICE_NAMES:
                raise StreamFailure("reserved device name in path", stream_id=stream_id)

        candidate = self.root.joinpath(*segments) if segments else self.root

        try:
            # resolve() follows symlinks, so a link pointing out of the root
            # is caught by the containment test below rather than followed.
            real = candidate.resolve()
        except OSError as exc:
            raise StreamFailure(
                f"cannot resolve path: {exc}", status=404, stream_id=stream_id
            ) from exc

        if real != self.root and self.root not in real.parents:
            raise StreamFailure("path escapes the document root", stream_id=stream_id)

        if real.is_dir():
            real = real / self.index
        if not real.is_file():
            raise StreamFailure(f"no such file: {path}", status=404, stream_id=stream_id)
        return real


@dataclass
class Config:
    idle_timeout: float = 30.0
    recv_size: int = 65536
    max_data: int = MAX_FRAME_SIZE
    max_requests_per_connection: int = 10_000
    max_body: int = DEFAULT_MAX_BODY


class Stats:
    def __init__(self):
        self._lock = threading.Lock()
        self.accepted = 0
        self.requests = 0
        self.responses = 0
        self.skipped_unknown = 0
        self.active = 0

    def bump(self, name: str, amount: int = 1) -> None:
        with self._lock:
            setattr(self, name, getattr(self, name) + amount)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "accepted": self.accepted,
                "requests": self.requests,
                "responses": self.responses,
                "skipped_unknown": self.skipped_unknown,
                "active": self.active,
            }


class Connection:
    """One TCP connection, many streams, exactly one preface."""

    def __init__(self, sock: socket.socket, addr, files: StaticFiles, config: Config, stats):
        self.sock = sock
        self.addr = addr
        self.files = files
        self.config = config
        self.stats = stats

    def run(self) -> None:
        reader = FrameReader(expect_preface=True)
        assembler = MessageAssembler("server", max_body=self.config.max_body)
        served = 0
        try:
            self.sock.settimeout(self.config.idle_timeout)
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

            while True:
                # Drain every frame already buffered before asking for more:
                # the same rule as Part A, and for the same reason.
                while True:
                    try:
                        frame = reader.next_frame()
                    except ConnectionFailure as failure:
                        self._fail_connection(failure)
                        return
                    if frame is None:
                        break

                    if not frame.known:
                        # SPEC 3.1. Nothing to do: the reader already stepped
                        # over exactly `length` bytes without understanding
                        # a single one of them.
                        log.info("skipped unknown frame type 0x%02x", frame.type)
                        self.stats.bump("skipped_unknown")
                        continue

                    try:
                        message = assembler.accept(frame)
                    except StreamFailure as failure:
                        self._send(
                            encode_response(
                                _error_response(failure.status, failure.reason),
                                failure.stream_id or frame.stream_id,
                                max_data=self.config.max_data,
                            )
                        )
                        continue
                    except ConnectionFailure as failure:
                        self._fail_connection(failure)
                        return

                    if message is None:
                        continue

                    served += 1
                    self.stats.bump("requests")
                    self._respond(message)
                    if served >= self.config.max_requests_per_connection:
                        return

                try:
                    data = self.sock.recv(self.config.recv_size)
                except TimeoutError:
                    log.info("%s idle timeout", self._peer())
                    return
                except OSError:
                    return
                if not data:
                    return
                reader.feed(data)
        finally:
            self._close()

    def _respond(self, request: Request) -> None:
        try:
            response = self._build(request)
        except StreamFailure as failure:
            response = _error_response(failure.status, failure.reason)
        except Exception:  # noqa: BLE001
            log.exception("failed to serve %s", request.path)
            response = _error_response(500, "internal error")

        log.info(
            "%s stream %d  %s %s -> %d",
            self._peer(),
            request.stream_id,
            request.method,
            request.path,
            response.status,
        )
        self._send(encode_response(response, request.stream_id, max_data=self.config.max_data))
        self.stats.bump("responses")

    def _build(self, request: Request) -> Response:
        if request.method not in ALLOWED_METHODS:
            return _error_response(405, f"{request.method} is not allowed on a static file")

        target = self.files.resolve(request.path, request.stream_id)
        payload = target.read_bytes()
        content_type, _ = mimetypes.guess_type(target.name)
        headers = [
            ("content-type", content_type or "application/octet-stream"),
            ("content-length", str(len(payload))),
            ("last-modified", formatdate(target.stat().st_mtime, usegmt=True)),
            ("date", formatdate(usegmt=True)),
            ("server", SERVER_TOKEN),
        ]
        # SPEC 5.6: HEAD keeps the content-length and drops the bytes.
        body = b"" if request.method == "HEAD" else payload
        return Response(status=200, headers=headers, body=body)

    def _fail_connection(self, failure: ConnectionFailure) -> None:
        log.warning("%s connection error: %s", self._peer(), failure.reason)
        with contextlib.suppress(OSError):
            self.sock.sendall(encode_error(failure.status, failure.reason).encode())

    def _send(self, frames) -> None:
        self.sock.sendall(b"".join(frame.encode() for frame in frames))

    def _close(self) -> None:
        for action in (lambda: self.sock.shutdown(socket.SHUT_WR), self.sock.close):
            with contextlib.suppress(OSError):
                action()
        self.stats.bump("active", -1)

    def _peer(self) -> str:
        return f"{self.addr[0]}:{self.addr[1]}"


def _error_response(status: int, reason: str) -> Response:
    body = f"{status} {reason}".encode()
    return Response(
        status=status,
        headers=[
            ("content-type", "text/plain; charset=utf-8"),
            ("content-length", str(len(body))),
            ("date", formatdate(usegmt=True)),
            ("server", SERVER_TOKEN),
        ],
        body=body,
    )


class Bserve:
    def __init__(
        self,
        root: str | Path,
        port: int = 9000,
        *,
        host: str = "127.0.0.1",
        config: Config | None = None,
    ):
        self.files = StaticFiles(Path(root))
        self.host = host
        self.port = port
        self.config = config or Config()
        self.stats = Stats()
        self._listener: socket.socket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sockets: set[socket.socket] = set()
        self._lock = threading.Lock()

    def start(self) -> Bserve:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name == "nt":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.host, self.port))
        listener.listen(64)
        listener.settimeout(0.25)
        self._listener = listener
        self.port = listener.getsockname()[1]
        self._thread = threading.Thread(target=self._accept_loop, daemon=True, name="bserve")
        self._thread.start()
        log.info("serving %s on bht://%s:%d", self.files.root, self.host, self.port)
        return self

    @property
    def address(self) -> tuple[str, int]:
        return (self.host, self.port)

    def serve_forever(self) -> None:
        try:
            while not self._stop.wait(0.5):
                pass
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        if self._listener:
            with contextlib.suppress(OSError):
                self._listener.close()
        with self._lock:
            sockets = list(self._sockets)
        for sock in sockets:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
        if self._thread:
            self._thread.join(3.0)
        log.info("stopped; %s", self.stats.snapshot())

    def __enter__(self) -> Bserve:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.shutdown()

    def _accept_loop(self) -> None:
        assert self._listener is not None
        while not self._stop.is_set():
            try:
                sock, addr = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            self.stats.bump("accepted")
            self.stats.bump("active")
            with self._lock:
                self._sockets.add(sock)
            threading.Thread(
                target=self._serve, args=(sock, addr), daemon=True, name="bserve-conn"
            ).start()

    def _serve(self, sock: socket.socket, addr) -> None:
        try:
            Connection(sock, addr, self.files, self.config, self.stats).run()
        except Exception:  # noqa: BLE001
            log.exception("connection handler crashed")
        finally:
            with self._lock:
                self._sockets.discard(sock)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bserve", description=__doc__)
    parser.add_argument("root", help="document root")
    parser.add_argument("port", nargs="?", type=int, default=9000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--idle-timeout", type=float, default=30.0)
    parser.add_argument("-q", "--quiet", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )

    try:
        server = Bserve(
            args.root, args.port, host=args.host, config=Config(idle_timeout=args.idle_timeout)
        )
    except (FileNotFoundError, NotADirectoryError) as exc:
        print(f"bserve: no such document root: {exc}", file=sys.stderr)
        return 2

    server.start()
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
