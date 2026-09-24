from __future__ import annotations

from dataclasses import dataclass, field


class Headers:

    __slots__ = ("_items", "_index")

    def __init__(self, items: list[tuple[str, str]] | None = None):
        self._items: list[tuple[str, str]] = list(items or [])
        self._index: dict[str, list[str]] = {}
        for name, value in self._items:
            self._index.setdefault(name.lower(), []).append(value)

    def get(self, name: str, default: str | None = None) -> str | None:
        values = self._index.get(name.lower())
        return values[0] if values else default

    def get_all(self, name: str) -> list[str]:
        return list(self._index.get(name.lower(), ()))

    def count(self, name: str) -> int:
        return len(self._index.get(name.lower(), ()))

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name.lower() in self._index

    def __iter__(self):
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __repr__(self) -> str:
        return f"Headers({self._items!r})"

    def tokens(self, name: str) -> set[str]:
        out: set[str] = set()
        for value in self.get_all(name):
            out.update(token.strip().lower() for token in value.split(",") if token.strip())
        return out


@dataclass(frozen=True)
class Request:
    method: str
    target: str
    version: str
    headers: Headers
    body: bytes = b""
    path: str = "/"
    query_string: str = ""
    framing: str = "length"
    raw_length: int = 0
    trailers: Headers = field(default_factory=Headers)

    @property
    def wants_close(self) -> bool:
        tokens = self.headers.tokens("connection")
        if self.version == "HTTP/1.0":
            return "keep-alive" not in tokens
        return "close" in tokens
