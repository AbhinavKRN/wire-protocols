from __future__ import annotations

import contextlib
import logging
import os
import socket
import threading

from .connection import Config, ConnectionHandler
from .routes import Router


class Stats:

    def __init__(self):
        self._lock = threading.Lock()
        self.accepted = 0
        self.responses = 0
        self.active = 0

    def record_accept(self) -> None:
        with self._lock:
            self.accepted += 1
            self.active += 1

    def record_response(self) -> None:
        with self._lock:
            self.responses += 1

    def record_disconnect(self) -> None:
        with self._lock:
            self.active -= 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {"accepted": self.accepted, "responses": self.responses, "active": self.active}


class Server:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8080,
        *,
        router: Router | None = None,
        config: Config | None = None,
        logger: logging.Logger | None = None,
        backlog: int = 64,
    ):
        self.host = host
        self.port = port
        self.router = router or Router()
        self.config = config or Config()
        self.log = logger or logging.getLogger("calcserver")
        self.backlog = backlog
        self.stats = Stats()

        self._listener: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._workers: set[threading.Thread] = set()
        self._live_sockets: set[socket.socket] = set()
        self._lock = threading.Lock()

    def start(self) -> Server:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name == "nt":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.host, self.port))
        listener.listen(self.backlog)
        listener.settimeout(0.25)
        self._listener = listener
        self.port = listener.getsockname()[1]

        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="calcserver-accept", daemon=True
        )
        self._accept_thread.start()
        self.log.info("listening on http://%s:%d", self.host, self.port)
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

    def shutdown(self, timeout: float = 3.0) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        if self._listener is not None:
            with contextlib.suppress(OSError):
                self._listener.close()
        with self._lock:
            sockets = list(self._live_sockets)
            workers = list(self._workers)
        for sock in sockets:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
        if self._accept_thread is not None:
            self._accept_thread.join(timeout)
        for worker in workers:
            worker.join(timeout)
        self.log.info("stopped; %s", self.stats.snapshot())

    def __enter__(self) -> Server:
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
            self.stats.record_accept()
            self.log.info("accepted connection #%d from %s", self.stats.accepted, addr)
            worker = threading.Thread(
                target=self._serve_connection,
                args=(sock, addr),
                name=f"calcserver-conn-{self.stats.accepted}",
                daemon=True,
            )
            with self._lock:
                self._workers.add(worker)
                self._live_sockets.add(sock)
            worker.start()

    def _serve_connection(self, sock: socket.socket, addr) -> None:
        handler = ConnectionHandler(sock, addr, self.router, self.config, self.stats, self.log)
        try:
            handler.run()
        except Exception:  # noqa: BLE001
            self.log.exception("connection handler crashed")
        finally:
            with self._lock:
                self._live_sockets.discard(sock)
                self._workers.discard(threading.current_thread())
