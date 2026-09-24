from __future__ import annotations


class HttpError(Exception):

    status = 500
    reason = "Internal Server Error"
    close = False

    def __init__(self, detail: str = "", *, headers: list[tuple[str, str]] | None = None):
        super().__init__(detail or self.reason)
        self.detail = detail
        self.headers = headers or []

    def body(self) -> bytes:
        text = f"{self.status} {self.reason}"
        if self.detail:
            text += f": {self.detail}"
        return text.encode("utf-8", "replace")


class BadRequest(HttpError):
    status = 400
    reason = "Bad Request"
    close = False


class NotFound(HttpError):
    status = 404
    reason = "Not Found"
    close = False


class MethodNotAllowed(HttpError):
    status = 405
    reason = "Method Not Allowed"
    close = False

    def __init__(self, detail: str = "", *, allow: tuple[str, ...] = ()):
        super().__init__(detail, headers=[("Allow", ", ".join(allow))] if allow else None)


class RequestTimeout(HttpError):
    status = 408
    reason = "Request Timeout"
    close = True


class MalformedMessage(BadRequest):

    close = True


class UriTooLong(HttpError):
    status = 414
    reason = "URI Too Long"
    close = True


class PayloadTooLarge(HttpError):
    status = 413
    reason = "Content Too Large"
    close = True


class HeaderFieldsTooLarge(HttpError):
    status = 431
    reason = "Request Header Fields Too Large"
    close = True


class NotImplementedError_(HttpError):
    status = 501
    reason = "Not Implemented"
    close = True


class VersionNotSupported(HttpError):
    status = 505
    reason = "HTTP Version Not Supported"
    close = True
