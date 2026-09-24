from __future__ import annotations


class BhttpError(Exception):

    def __init__(self, reason: str, *, status: int = 400):
        super().__init__(reason)
        self.reason = reason
        self.status = status


class ConnectionFailure(BhttpError):
    pass


class StreamFailure(BhttpError):

    def __init__(self, reason: str, *, status: int = 400, stream_id: int = 0):
        super().__init__(reason, status=status)
        self.stream_id = stream_id
