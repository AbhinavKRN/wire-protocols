# BHT/1 — HTTP, in binary

Two programs and a specification. `bserve` hands out files, `bcurl` fetches
them, and the only thing that is supposed to pass between them is
[SPEC.md](SPEC.md).

- [SPEC.md](SPEC.md) — the protocol, normative, two pages.
- [HEXDUMP.md](HEXDUMP.md) — one real request and response, annotated byte by
  byte, generated from captured traffic.
- `bhttp/` — the codec: frames, headers, messages. No sockets in it.
- `bserve.py`, `bcurl.py` — the two tracks.

## Run it

```sh
./bserve ./www 9000                          # bserve.cmd on Windows
./bcurl -v localhost:9000/index.html         # bcurl.cmd on Windows

./bcurl localhost:9000/hello.txt localhost:9000/docs/nested.txt
python tools/capture.py                      # regenerate HEXDUMP.md
python -m pytest                             # from the repo root
```

`bcurl` exits 0 on success, 4 on a 4xx, 5 on a 5xx, 7 if it never got a
connection, 8 if the peer is not speaking BHT/1, and 2 on a usage error.

## The frame header, and why it is these widths

```
+---------------+---------------+---------------+---------------+
|                    Length (24)                |    Type (8)   |
+---------------+---------------+---------------+---------------+
|    Flags (8)  | Reserved (8)  |         Stream ID (16)        |
+---------------+---------------+---------------+---------------+
```

HTTP/2 chose 24 / 8 / 8 / 1+31 in nine bytes. Mine is 24 / 8 / 8 / 8 / 16 in
eight. Field by field:

**Length, 24 bits.** Sixteen bits caps a payload at 64 KiB, which is fine
until you meet a 100 KiB image and have to invent continuation frames to
carry it. Thirty-two bits means a peer can announce a 4 GiB frame and watch
me try to allocate it — a denial of service that costs the attacker eight
bytes. Twenty-four bits is where HTTP/2 landed for the same reason, and I add
what HTTP/2 adds too: a `MAX_FRAME_SIZE` of 65535 that a receiver enforces
*on the header*, before a single payload byte is buffered. The field is wide
enough to grow into and the policy is what keeps it honest. (Test:
`test_oversized_frame_length_is_refused_without_buffering`.)

**Length comes first, before Type.** This is the single most load-bearing
decision in the layout. A receiver must be able to step over a frame it does
not understand, so the number of bytes to step over cannot be stored in a
place whose meaning depends on the type. Put the type first and
"skip unknown frames" becomes "skip unknown frames whose length you happen to
know how to find", which is not a rule at all.

**Type, 8 bits.** Four are assigned and 251 are free. A nibble would have
been enough for v1 and catastrophic for v2; a byte is the smallest unit that
does not require bit-shifting to read, and shifting to read a *type* is how
you end up with a parser nobody wants to modify.

**Flags, 8 bits, with one assigned.** `END_MESSAGE` could have been a
separate frame type, and then every future type would need an "…and this one
ends the message" twin. As a flag it composes: it means the same thing on a
REQUEST frame with no body as it does on the last DATA frame of a 10 MB one.
Unassigned bits MUST be ignored on receipt, which is the second
forward-compatibility lever after unknown types.

**Stream ID, 16 bits** — and this is the width I would be challenged on.
HTTP/2 spends 31 bits because a browser holds one connection to a host open
for hours and opens streams for every asset, and because it needed a reserved
bit it later regretted. Sixty-five thousand exchanges on one connection is
generous for fetching files, and when it is not, reconnecting costs one
handshake. What the narrow field *does* cost is a rule I am then obliged to
write down: IDs must not wrap, and a client that reaches 65535 closes and
reopens (SPEC 4.4). A 16-bit counter with no exhaustion rule would be a bug
waiting for a long-lived connection to find it.

**Reserved, 8 bits.** Here is the honest version: dropping stream IDs from 31
bits to 16 left me at seven bytes. Seven is an awkward number to read, to
document and to test, and the choice was between shrinking something useful
or spending the byte on the future. I spent it, wrote "MUST be ignored on
receipt" next to it, and shipped a test that sets it to `0xFF` and expects
the request to be served anyway. A reserved field that receivers have never
been observed to ignore is not reserved; it is a landmine for v2.

**Big-endian**, because every other protocol on the wire is, and a hexdump
that reads left to right in the same order as the field values is worth more
than the nothing that little-endian would save.

## Extensibility, which is the part that is actually tested

SPEC 3.1: *a receiver meeting a frame type it does not know MUST skip it
cleanly.* Three mechanisms implement the same idea at three scales:

| Lever | Rule | Test |
|---|---|---|
| Unknown frame type | read Length, discard, continue | `test_unknown_frame.py` |
| Unassigned flag bits | ignore | `test_unassigned_flag_bits_and_reserved_byte_are_ignored` |
| Reserved byte | ignore | same |
| Unassigned header prefixes `0x01`–`0x7F` | reject, reserved for a dynamic table | `invalid.jsonl` |

The fourth row is deliberately the opposite of the first three. A frame type
I do not know is someone else's business and I can step around it. A *header
entry* prefix I do not know is inside a structure I am mid-way through
parsing, and guessing would mean parsing the rest of that block wrong. Skip
what you can measure; refuse what you cannot.

`test_unknown_frame.py` sends type `0x7F` between two requests, in the same
packet as a request, in the middle of a message's DATA sequence, and with a
10 KB payload — and also stands up a fake "server from the future" that
sprays unknown frames at `bcurl`. An extensibility clause with no test is a
wish.

## Headers: HPACK's first two mechanisms

Number the ten names you actually send; length-prefix everything else.

A response from `bserve` carries `content-type`, `content-length`,
`last-modified`, `date` and `server` — five headers, five index bytes, zero
bytes of name text. In HTTP/1.1 those names cost 63 bytes of ASCII on every
single response. Values stay text, because values are where the entropy is
and compressing them buys little for the complexity.

What is deliberately missing is HPACK's third mechanism, the dynamic table.
It is where the real compression is, and also where the state, the
synchronisation bugs and the CRIME-class attacks live. The `0x01`–`0x7F`
prefix range is left unassigned so that v2 has somewhere to put it without
moving anything else.

## Bodies need no chunked encoding

This is the punchline of doing Part A first. HTTP/1.1's hardest problem —
where does this message end, when the connection is not going to close — took
a whole encoding layer to solve: chunk sizes in hex, terminators, trailers, a
second state machine, and a decade of smuggling vulnerabilities where two
implementations disagreed about the answer.

Here it is one bit. Every frame states its length, and the last frame of a
message sets `END_MESSAGE`. Streaming a file of unknown length is just
sending DATA frames until you stop. There is no terminator to scan for, so
there is nothing to disagree about.

`content-length` still exists as a header, but SPEC 5.5 makes it *checked,
not trusted*: it must agree with the bytes delivered, and a mismatch is a
400. In HTTP/1.1 that field decides framing; here framing decides it. The
whole class of "which of these two length declarations do we believe" is
gone, because there is only one.

## Errors: the same split as Part A

`StreamFailure` is answerable — the frame was well formed, the next frame
starts exactly where the header said, so the peer gets a 400/404/405/413 on
that stream and the connection carries on. `ConnectionFailure` means frame
boundaries are in doubt (bad preface, oversized length, stream-ID reuse, a
role violation), so the peer gets one ERROR frame on stream 0 and then the
connection goes away. That is what the ERROR frame type is *for*: a failure
that cannot be attributed to a stream cannot be reported as a response to
one.

A rejected head whose body is still arriving is drained and discarded rather
than treated as fatal, so one bad message never costs the connection.

## Interop, which is what the brief actually grades

> A client that only works against your own server is an implementation, not
> a protocol.

`bserve` and `bcurl` agreeing proves almost nothing: they share a codec, so
they are wrong in the same places. The real conformance suite is
[`tests/vectors/`](tests/vectors): hex literals with their decoded meaning
written out **by hand**, plus 25 must-reject vectors covering every item in
SPEC section 9. The decoder is tested against those bytes, never against our
own encoder, and vectors marked `canonical` run the other way too — the
encoder must reproduce the literal byte for byte, which is what catches an
encoder and decoder that are wrong in the same way.

Anyone implementing from SPEC.md can run the same three files against their
own code. That is the deliverable that makes this a protocol.

[`tests/test_spec.py`](tests/test_spec.py) goes one step further and parses
SPEC.md itself, checking that the frame type codes, method codes, static
table and limits it publishes are the ones the code uses. A spec that has
drifted from its implementation is worse than no spec, because the stranger
reading it has no way to find out.

## Security notes for `bserve`

The peer supplies a *name*, never a path. Rejected: dot segments before and
after percent-decoding, backslashes, control characters and `%00`, drive
letters and NTFS alternate-data-stream markers (`/C:/…`, `file.txt:$DATA`),
Windows device names (`con`, `nul`, `lpt1`, with or without an extension),
and anything whose resolved real path is not inside the resolved root — which
is what catches a symlink pointing out of the document root. Decoding happens
exactly once, so `%252e%252e` is a filename, not an escape.
[`tests/test_security.py`](tests/test_security.py) builds every one of these
frames by hand, because our own encoder refuses to construct them and an
attacker is not using our encoder.

## What version 2 should change

- A SETTINGS frame, so `MAX_FRAME_SIZE` and the body limit are negotiated
  rather than assumed. Type `0x05` is free and v1 receivers will skip it.
- HPACK's dynamic table, in the `0x01`–`0x7F` header prefix range.
- Flow control. Right now a peer can stream an 8 MB body at a receiver that
  never asked for it; the limit is a cap, not back-pressure.
- Server push, which is why even stream IDs are already reserved.

None of those require a new preface, which is the test of whether the
extension points were put in the right places.
