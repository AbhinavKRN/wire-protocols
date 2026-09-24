# A calculator that stays on the line

An HTTP/1.1 server in Python with nothing but `socket`. Four arithmetic
routes, and underneath them the only question the assignment is really
asking: **where does this request end and the next one begin?**

## Run it

```sh
python -m calcserver --port 8080          # from this directory
python marking_script.py 8080             # the brief's script, in another shell
python -m pytest                          # or: python -m unittest discover -s tests
```

## What it answers

| Request | Status | Body |
|---|---|---|
| `GET /add?a=2&b=3` | 200 | `5` |
| `GET /sub?a=10&b=4` | 200 | `6` |
| `GET /mul?a=6&b=7` | 200 | `42` |
| `GET /div?a=9&b=3` | 200 | `3` |
| `GET /div?a=1&b=0` | 400 | division by zero |
| `GET /add?a=x&b=3` | 400 | not an integer |
| `GET /pow?a=2&b=8` | 404 | no such operation |
| `POST /add` | 405 | `Allow: GET, HEAD` |
| `GET /add` without `Host` | 400 | HTTP/1.1 requires one |

All of it on one connection, one handshake, in any delivery shape: one byte
at a time, six requests in a single packet, or anything in between.

## The design, in one idea

HTTP/1.0 could find the end of a message by hanging up. Keeping the
connection open takes that away, so the end of a message has to be computed
from the bytes themselves, before you are allowed to read the next one. That
computation is a pure function of the buffer, so it lives in a module with no
sockets in it at all:

```
calcserver/http/parser.py     find message boundaries    (no I/O)
calcserver/http/response.py   serialise                  (no I/O)
calcserver/routes.py          arithmetic                 (no I/O)
calcserver/connection.py      the socket loop
calcserver/server.py          accept, threads, lifecycle
```

`RequestParser` exposes two methods. `feed(data)` takes whatever `recv()`
happened to return. `next_request()` returns the next complete request and
consumes *exactly* its bytes, or returns `None` meaning "not knowable yet".
That forces the connection loop into the only correct shape:

```python
while True:
    while (request := parser.next_request()) is not None:   # drain
        respond(request)
    data = sock.recv(65536)                                 # only then
    if not data:
        break
    parser.feed(data)
```

Calling `recv()` while a complete message is still in the buffer is the bug,
and this loop cannot express it. **Pipelining is not a feature that was added
to this server; it is what that loop already does** when six requests arrive
in one packet. The stretch goal was free because the framing was right.

## Decisions I would defend in review

**Two classes of 400.** A malformed `Content-Length` and a missing `Host` are
both "400 Bad Request", and they are not remotely the same event. After a
missing `Host` the message was still perfectly framed: we know where it
ended, so we answer and stay on the line. After a bad `Content-Length` we do
not know where the body stopped, so every subsequent byte is a guess —
answer, then hang up. `HttpError.close` carries that distinction and the
connection layer obeys it.

**Semantic errors are deferred until the body is drained.** A missing `Host`
is discovered while parsing the head, but the body is still on the wire.
Raising immediately would leave those body bytes to be read as the next
request line, so one bad request would corrupt every request after it. The
parser records the error on the pending message and raises it only once the
body has been consumed (`_Head.deferred_error`). `DeferredErrorTests` pins
this down.

**A body is never a request.** `test_a_body_is_not_a_request` posts a body
whose contents are a valid-looking `GET /mul?a=6&b=7`, and asserts the server
replies exactly once. Replying twice is request smuggling, and it is what a
parser that scans for `\r\n\r\n` without honouring `Content-Length` does.

**Strictness where ambiguity is an attack.** Duplicate `Content-Length`,
`Content-Length` together with `Transfer-Encoding`, bare LF line endings,
obsolete line folding, whitespace before a colon, and two `Host` headers are
all rejected with a hang-up. Each one is a case where two implementations can
disagree about where a message ended, which is the entire request-smuggling
class. Tolerance here is not politeness, it is a vulnerability.

**Bare LF fails fast rather than timing out.** A peer sending LF-terminated
lines will never send our `CRLFCRLF`, so a strict parser would sit there
until the idle timeout. `_take_head` looks for `\n\n` too and rejects the
moment the evidence arrives.

**Every limit is a DoS bound.** `Limits` caps the request line, head size,
header count, body size, and even how many blank lines may precede a request.
A parser without limits buffers until the process dies, on the word of a peer
who has not finished a sentence.

**Path is checked before method**, so `POST /pow` is 404 and `POST /add` is
405: you cannot be told which methods a resource allows before establishing
that the resource exists.

**Unknown query parameters are rejected.** `?a=1&b=2&c=3` is a 400. Silently
ignoring `c` answers a question the client did not ask.

**Idle connections are closed quietly; half-sent ones get 408.** A client
that is merely idle may be about to reuse the connection, and racing it with
a 408 helps nobody. A client that sent half a request and stopped gets told
why.

**HEAD is supported**, because a server that supports GET should. Its
response carries the `Content-Length` of the body it is forbidden to send —
which is exactly the trap my own test client fell into first.

**Thread per connection.** An event loop would scale further, but it would
smear the framing state machine across a dispatcher, and the framing state
machine is the point. Because the parser is I/O-free, replacing `server.py`
with a `selectors` loop would not change a line of how messages are found.

## Test map

| File | What it holds down |
|---|---|
| `tests/test_parser.py` | 40 pure-parser cases: boundaries, limits, chunked, deferred errors |
| `tests/test_conformance.py` | the marking script; the full feature table; 1 handshake / 6 responses |
| `tests/test_adversarial.py` | delivery shapes, smuggling, keep-alive lifetime, hostile input, 12 concurrent clients |

Notable entries: `test_one_byte_at_a_time` (no chunk boundary coincides with
anything), `test_pipelining_all_six_in_one_write`, and
`test_request_split_across_the_header_terminator`, which cuts the request
between the two CRLFs of the terminator.

## Stretch goals

All four, and all tested:

- `Connection: close` honoured, plus HTTP/1.0's inverted default;
- an idle timeout, with 408 for a half-sent request and a quiet close for a
  merely idle one, and `max_requests_per_connection` as a second bound;
- chunked request decoding, including extensions and trailers, with
  `test_chunked_body_is_consumed_and_the_stream_stays_aligned` proving the
  stream survives it;
- pipelining: all six at once, answered in order.
