"""The spec is the deliverable, so it is treated as a source file.

These tests parse SPEC.md and compare the constants it publishes against the
ones the code uses. A spec that has drifted from the implementation is worse
than no spec, because a stranger implementing from it has no way to know.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from bhttp.frame import MAX_FRAME_SIZE, FrameType
from bhttp.headers import STATIC_TABLE
from bhttp.messages import MAX_PATH, Method

SPEC = (Path(__file__).resolve().parent.parent / "SPEC.md").read_text(encoding="utf-8")


class SpecMatchesCodeTests(unittest.TestCase):
    def test_frame_type_codes(self):
        published = {
            name: int(code, 16)
            for code, name in re.findall(
                r"^\| `0x([0-9a-f]{2})` \| (REQUEST|RESPONSE|DATA|ERROR) \|", SPEC, re.MULTILINE
            )
        }
        self.assertEqual(published, {member.name: int(member) for member in FrameType})

    def test_method_codes(self):
        published = {
            name: int(code, 16)
            for name, code in re.findall(r"^\| (\w+) \| `0x([0-9a-f]{2})` \|", SPEC, re.MULTILINE)
        }
        self.assertEqual(published, {member.name: int(member) for member in Method})

    def test_static_table(self):
        published: dict[int, str] = {}
        for row in re.findall(
            r"^\| (\d+) \| `([a-z-]+)` \| (\d+) \| `([a-z-]+)` \|$", SPEC, re.MULTILINE
        ):
            published[int(row[0])] = row[1]
            published[int(row[2])] = row[3]
        self.assertEqual(published, {i: name for i, name in enumerate(STATIC_TABLE) if i})

    def test_limits(self):
        self.assertIn(f"| `MAX_FRAME_SIZE` (payload) | {MAX_FRAME_SIZE} bytes |", SPEC)
        self.assertIn(f"| Maximum path length | {MAX_PATH} bytes |", SPEC)

    def test_the_skip_rule_is_still_a_must(self):
        """If this sentence ever softens, the extensibility story is gone."""
        self.assertRegex(
            SPEC, r"receiver that meets a frame type it does not know MUST skip it\s+cleanly"
        )

    def test_every_conformance_item_has_a_vector(self):
        """SPEC section 9 promises machine-readable vectors for its list."""
        items = re.findall(r"^\d+\. ", SPEC[SPEC.index("## 9. Conformance") :], re.MULTILINE)
        vectors = (
            Path(__file__).resolve().parent / "vectors" / "invalid.jsonl"
        ).read_text().strip().splitlines()
        # 13 items in the list, the last of which is the must-*not*-reject one.
        self.assertEqual(len(items), 13)
        self.assertGreaterEqual(len(vectors), len(items) - 1)


if __name__ == "__main__":
    unittest.main()
