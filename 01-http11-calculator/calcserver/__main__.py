"""python -m calcserver [--port 8080]"""

from __future__ import annotations

import argparse
import logging
import signal
import sys

from .connection import Config
from .server import Server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="calcserver", description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--idle-timeout", type=float, default=15.0)
    parser.add_argument("--max-requests", type=int, default=1000)
    parser.add_argument("-q", "--quiet", action="store_true", help="only log warnings")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )

    server = Server(
        args.host,
        args.port,
        config=Config(
            idle_timeout=args.idle_timeout,
            max_requests_per_connection=args.max_requests,
        ),
    )
    server.start()

    def handle_signal(signum, frame):  # noqa: ARG001
        server.shutdown()

    signal.signal(signal.SIGINT, handle_signal)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
