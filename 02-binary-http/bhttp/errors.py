"""Failure taxonomy for BHT/1.

Same split as Part A, for the same reason: some failures cost you the
message, and some cost you the connection. The difference is whether the
receiver still knows where the next frame starts.
"""

from __future__ import annotations


class BhttpError(Exception):
    """Base class. ``status`` is what goes on the wire."""

    def __init__(self, reason: str, *, status: int = 400):
        super().__init__(reason)
        self.reason = reason
        self.status = status


class ConnectionFailure(BhttpError):
    """Unrecoverable: send ERROR on stream 0, then close.

    Raised when frame boundaries are in doubt, or when the peer has broken a
    connection-wide rule (bad preface, oversized frame, stream ID reuse).
    """


class StreamFailure(BhttpError):
    """Recoverable: answer this stream with ``status``, keep the connection.

    The frame was well-formed, so the next frame starts exactly where the
    header said it would.
    """

    def __init__(self, reason: str, *, status: int = 400, stream_id: int = 0):
        super().__init__(reason, status=status)
        self.stream_id = stream_id
