# Wire protocols: HTTP/1.1 framing, and a binary HTTP of my own

Two pieces of coursework that are really one question asked twice.

| | |
|---|---|
| [`01-http11-calculator/`](01-http11-calculator/) | An HTTP/1.1 calculator that stays on the line. Four arithmetic routes over a keep-alive connection. |
| [`02-binary-http/`](02-binary-http/) | BHT/1: my own binary HTTP. A [spec](02-binary-http/SPEC.md), a server, a client, and an [annotated hexdump](02-binary-http/HEXDUMP.md). |

Python 3.11+, standard library only. No frameworks, no dependencies, and no
`http.server` — just `socket`.

## Run everything

```powershell
.\tasks.ps1 test        # every test in both projects
.\tasks.ps1 stdlib      # the same, via unittest, if pytest is not installed
.\tasks.ps1 demo        # bserve + bcurl -v, end to end
```

Or by hand:

```sh
cd 01-http11-calculator && python -m calcserver --port 8080
python marking_script.py 8080                  # the brief's script

cd 02-binary-http && ./bserve ./www 9000       # bserve.cmd on Windows
./bcurl -v localhost:9000/index.html
```

## The through-line

HTTP/1.0 never had to answer the question "where does this message end?"
Hanging up *was* the answer, and it was free. The moment you keep the
connection open, that answer is gone and you have to compute the end of a
message from the bytes themselves, before you are allowed to look at the next
one.

**Part A** answers it the way HTTP/1.1 does: scan for a blank line, read the
headers, believe `Content-Length`, and consume exactly that many bytes and
not one more. The awkwardness of that answer is the whole lesson — the
delicate interaction of `Content-Length` and `Transfer-Encoding`, an entire
chunked-encoding layer for bodies whose length you do not know yet, and the
request-smuggling vulnerability class that exists because two
implementations can read the same bytes and disagree about where the message
stopped. There is a test for that case in each project.

**Part B** answers it the way HTTP/2 does, by putting the length in front of
every frame and spending one flag bit on "this is the last one". Chunked
encoding does not need to be implemented, because there is nothing left for
it to do. `content-length` survives only as a header that gets *checked*
against the framing rather than defining it.

That is the thing worth taking away: HTTP/2 is not binary because binary is
faster. It is binary because self-delimiting frames make a class of
ambiguity, and therefore a class of vulnerability, impossible to express.

## What is here

```
01-http11-calculator/
  calcserver/http/parser.py     message framing, no I/O          <- the assignment
  calcserver/connection.py      the drain loop, hence pipelining
  calcserver/server.py          accept, threads, lifecycle
  marking_script.py             the brief's script, runnable
  tests/                        70 tests

02-binary-http/
  SPEC.md                       the protocol                     <- the deliverable
  HEXDUMP.md                    a real exchange, annotated
  bhttp/                        frames, headers, messages; no I/O
  bserve.py  bcurl.py           the two tracks
  tests/vectors/*.jsonl         hand-written conformance vectors
  tests/                        88 tests
```

Both projects are built the same way: the part that decides where a message
ends is a pure function over a buffer, in a module with no sockets in it, and
the socket loop is a thin thing wrapped around it. That is what makes "what
if the body arrives one byte at a time" a unit test instead of a network
condition you have to arrange.

## Results

```
158 tests, 107 subtests, all passing
```

The ones worth looking at:

| Test | What it would catch |
|---|---|
| `test_a_body_is_not_a_request` | a POST body containing a valid-looking request being answered as one — request smuggling |
| `test_one_byte_at_a_time` | any assumption that packet boundaries mean anything |
| `test_pipelining_all_six_in_one_write` | `recv()` being called while a complete message is already buffered |
| `test_missing_host_is_raised_only_after_the_body_is_consumed` | one bad request desynchronising every request after it |
| `test_unknown_frame_between_two_requests` | a v1 receiver that cannot survive a v2 sender |
| `test_invalid_vectors_are_rejected` | 25 hostile inputs SPEC section 9 promises to refuse |
| `test_spec.py` | the spec and the implementation drifting apart |

Design decisions and their justifications are in each project's README:
[Part A](01-http11-calculator/README.md), [Part B](02-binary-http/README.md).
