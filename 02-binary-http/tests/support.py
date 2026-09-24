from __future__ import annotations

import contextlib
import logging
import select
import socket
from pathlib import Path

from bhttp.frame import PREFACE, Frame, FrameReader
from bserve import Bserve, Config

logging.getLogger("bserve").addHandler(logging.NullHandler())
logging.getLogger("bserve").propagate = False

WWW = Path(__file__).resolve().parent.parent / "www"


class ServerFixture:

    def __init__(self, root: str | Path = WWW, **config_kwargs):
        self.server = Bserve(root, 0, config=Config(**config_kwargs))

    def __enter__(self) -> Bserve:
        return self.server.start()

    def __exit__(self, *exc) -> None:
        self.server.shutdown()


class RawPeer:

    def __init__(self, address: tuple[str, int], *, preface: bool = True, timeout: float = 5.0):
        self.sock = socket.create_connection(address, timeout=timeout)
        self.sock.settimeout(timeout)
        self.reader = FrameReader()
        if preface:
            self.sock.sendall(PREFACE)

    def send(self, data: bytes) -> None:
        self.sock.sendall(data)

    def send_frames(self, frames) -> None:
        self.sock.sendall(b"".join(frame.encode() for frame in frames))

    def read_frame(self) -> Frame:
        while True:
            frame = self.reader.next_frame()
            if frame is not None:
                return frame
            data = self.sock.recv(65536)
            if not data:
                raise ConnectionResetError("peer closed the connection")
            self.reader.feed(data)

    def read_message_frames(self) -> list[Frame]:
        frames = [self.read_frame()]
        while not frames[-1].end_message:
            frames.append(self.read_frame())
        return frames

    def body_of(self, frames: list[Frame]) -> bytes:
        return b"".join(frame.payload for frame in frames[1:])

    def is_open(self, wait: float = 0.3) -> bool:
        if self.reader.buffered:
            return True
        readable, _, _ = select.select([self.sock], [], [], wait)
        if not readable:
            return True
        return bool(self.sock.recv(1, socket.MSG_PEEK))

    def wait_until_closed(self, timeout: float = 3.0) -> bool:
        self.sock.settimeout(timeout)
        try:
            while True:
                if not self.sock.recv(65536):
                    return True
        except (TimeoutError, OSError):
            return False

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.sock.close()

    def __enter__(self) -> RawPeer:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def unhex(text: str) -> bytes:
    return bytes.fromhex(text.replace(" ", ""))
