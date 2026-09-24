# One BHT/1 exchange, annotated byte by byte

Captured from a live `bserve`/client exchange on 2026-09-22 18:05:39 UTC by
[`tools/capture.py`](tools/capture.py), and rendered by the same annotator
`bcurl -v` uses. Regenerate with `python tools/capture.py`.

The exchange is `GET /hello.txt` on stream 1, over one connection that is still
open when the dump ends.

## Request (client to server)

56 bytes on the wire.

### Raw

```
0000  42 48 54 31 00 00 2c 01 01 00 00 01 01 00 0a 2f  |BHT1..,......../|
0010  68 65 6c 6c 6f 2e 74 78 74 02 83 00 0f 6c 6f 63  |hello.txt....loc|
0020  61 6c 68 6f 73 74 3a 35 34 31 39 35 87 00 09 62  |alhost:54195...b|
0030  63 75 72 6c 2f 31 2e 30                          |curl/1.0|
```

### Annotated

```
0000  42 48 54 31                                           Connection preface "BHT1"
0004  00 00 2c                                              Length = 44
0007  01                                                    Type = 0x01 REQUEST
0008  01                                                    Flags = 0x01 END_MESSAGE
0009  00                                                    Reserved, MUST be ignored on receipt
000a  00 01                                                 Stream ID = 1
000c  01                                                    Method = 0x01 GET
000d  00 0a                                                 Path length = 10
000f  2f 68 65 6c 6c 6f 2e 74 78 74                         Path = "/hello.txt"
0019  02                                                    Header count = 2
001a  83                                                    Static index 3 -> "host"
001b  00 0f                                                 Value length = 15
001d  6c 6f 63 61 6c 68 6f 73 74 3a 35 34 31 39 35          Value = "localhost:54195"
002c  87                                                    Static index 7 -> "user-agent"
002d  00 09                                                 Value length = 9
002f  62 63 75 72 6c 2f 31 2e 30                            Value = "bcurl/1.0"
```

## Response (server to client)

129 bytes on the wire.

### Raw

```
0000  00 00 62 02 00 00 00 01 00 c8 05 82 00 0a 74 65  |..b...........te|
0010  78 74 2f 70 6c 61 69 6e 81 00 02 31 35 89 00 1d  |xt/plain...15...|
0020  54 75 65 2c 20 32 32 20 53 65 70 20 32 30 32 36  |Tue, 22 Sep 2026|
0030  20 31 37 3a 34 39 3a 33 31 20 47 4d 54 84 00 1d  | 17:49:31 GMT...|
0040  54 75 65 2c 20 32 32 20 53 65 70 20 32 30 32 36  |Tue, 22 Sep 2026|
0050  20 31 38 3a 30 35 3a 33 39 20 47 4d 54 85 00 0a  | 18:05:39 GMT...|
0060  62 73 65 72 76 65 2f 31 2e 30 00 00 0f 03 01 00  |bserve/1.0......|
0070  00 01 48 65 6c 6c 6f 2c 20 77 6f 72 6c 64 21 0d  |..Hello, world!.|
0080  0a                                               |.|
```

### Annotated

```
0000  00 00 62                                              Length = 98
0003  02                                                    Type = 0x02 RESPONSE
0004  00                                                    Flags = 0x00 none
0005  00                                                    Reserved, MUST be ignored on receipt
0006  00 01                                                 Stream ID = 1
0008  00 c8                                                 Status = 200
000a  05                                                    Header count = 5
000b  82                                                    Static index 2 -> "content-type"
000c  00 0a                                                 Value length = 10
000e  74 65 78 74 2f 70 6c 61 69 6e                         Value = "text/plain"
0018  81                                                    Static index 1 -> "content-length"
0019  00 02                                                 Value length = 2
001b  31 35                                                 Value = "15"
001d  89                                                    Static index 9 -> "last-modified"
001e  00 1d                                                 Value length = 29
0020  54 75 65 2c 20 32 32 20 53 65 70 20 32 30 32 36 ... (+13)  Value = "Tue, 22 Sep 2026 17:49:31 GMT"
003d  84                                                    Static index 4 -> "date"
003e  00 1d                                                 Value length = 29
0040  54 75 65 2c 20 32 32 20 53 65 70 20 32 30 32 36 ... (+13)  Value = "Tue, 22 Sep 2026 18:05:39 GMT"
005d  85                                                    Static index 5 -> "server"
005e  00 0a                                                 Value length = 10
0060  62 73 65 72 76 65 2f 31 2e 30                         Value = "bserve/1.0"
006a  00 00 0f                                              Length = 15
006d  03                                                    Type = 0x03 DATA
006e  01                                                    Flags = 0x01 END_MESSAGE
006f  00                                                    Reserved, MUST be ignored on receipt
0070  00 01                                                 Stream ID = 1
0072  48 65 6c 6c 6f 2c 20 77 6f 72 6c 64 21 0d 0a          Body bytes (15)
```

## What the bytes show

**The preface is not a frame.** Four bytes, once per connection, before
anything else. A peer that opens with `GET / HTTP/1.1` is rejected on byte one
instead of being misread as a frame header whose length field happens to say
4,670,532.

**Every frame begins with its length.** Type is the *fourth* byte, not the
first, so a receiver knows how far to jump before it knows what it is jumping
over. That ordering is the whole of the forward-compatibility rule in SPEC 3.1.

**The reserved byte is on the wire and means nothing.** It is there so that
version 2 has a field to spend without moving anything, and v1 receivers are
required to ignore whatever appears in it.

**The response head carries five headers and not one literal name.** Every
name bserve sends is in the ten-entry static table, so each costs a single
byte instead of its spelling: `content-type` is `0x82`, `content-length` is
`0x81`. The values are still text because values are where the entropy is.

**The body is its own frame.** The response head ends, a DATA frame follows
with its own length and the END_MESSAGE flag, and the message is over. No
chunked encoding, no terminator to scan for, no ambiguity about where the next
message starts -- the 15-byte body was framed by the same 8 bytes that
frame everything else.
