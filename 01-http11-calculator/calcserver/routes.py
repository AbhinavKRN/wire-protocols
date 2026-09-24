"""The calculator itself -- the least interesting file in the project.

Routing decisions worth defending:

* Path is checked before method, so ``POST /pow`` is a 404 and ``POST /add``
  is a 405. You cannot be told which methods a resource allows until it is
  established that the resource exists.
* Unknown query parameters are rejected rather than ignored. This is a
  teaching server for a strict protocol; silently discarding ``?a=1&bb=2``
  would answer a question the client did not ask.
* Operands are bounded in length. Python integers are arbitrary precision,
  so ``a`` with a million digits is a free CPU burn for the sender.
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import unquote_plus

from .http.errors import BadRequest, MethodNotAllowed, NotFound
from .http.message import Request
from .http.response import Response

ALLOWED_METHODS = ("GET", "HEAD")
MAX_OPERAND_DIGITS = 1000

Operation = Callable[[int, int], float | int]


def _div(a: int, b: int) -> float | int:
    if b == 0:
        raise BadRequest("division by zero")
    return a // b if a % b == 0 else a / b


OPERATIONS: dict[str, Operation] = {
    "/add": lambda a, b: a + b,
    "/sub": lambda a, b: a - b,
    "/mul": lambda a, b: a * b,
    "/div": _div,
}


class Router:
    def dispatch(self, request: Request) -> Response:
        operation = OPERATIONS.get(request.path)
        if operation is None:
            raise NotFound(f"no such operation: {request.path}")
        if request.method not in ALLOWED_METHODS:
            raise MethodNotAllowed(
                f"{request.method} is not allowed on {request.path}", allow=ALLOWED_METHODS
            )

        params = parse_query(request.query_string)
        unexpected = set(params) - {"a", "b"}
        if unexpected:
            raise BadRequest(f"unexpected parameter(s): {', '.join(sorted(unexpected))}")

        a = require_int(params, "a")
        b = require_int(params, "b")
        result = operation(a, b)
        return Response(status=200, body=format_number(result).encode("ascii"))


def parse_query(query_string: str) -> dict[str, str]:
    if not query_string:
        return {}
    params: dict[str, str] = {}
    for segment in query_string.split("&"):
        if not segment:
            continue
        name, sep, value = segment.partition("=")
        if not sep or not name:
            raise BadRequest(f"malformed query segment: {segment!r}")
        name = unquote_plus(name)
        if name in params:
            raise BadRequest(f"parameter {name!r} given more than once")
        params[name] = unquote_plus(value)
    return params


def require_int(params: dict[str, str], name: str) -> int:
    if name not in params:
        raise BadRequest(f"missing required parameter {name!r}")
    raw = params[name]
    digits = raw[1:] if raw.startswith("-") else raw
    if not digits or not digits.isdigit() or not digits.isascii():
        raise BadRequest(f"parameter {name!r} is not an integer: {raw!r}")
    if len(digits) > MAX_OPERAND_DIGITS:
        raise BadRequest(f"parameter {name!r} has more than {MAX_OPERAND_DIGITS} digits")
    return int(raw)


def format_number(value: float | int) -> str:
    if isinstance(value, int):
        return str(value)
    if value.is_integer():
        return str(int(value))
    return repr(round(value, 10))
