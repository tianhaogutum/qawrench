"""Archive-level disk contracts using independently encoded end records."""

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import re
import struct
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from .test_zip_headers import synthetic_zip
from qawrench.trace_budget import ArchiveError, compare_archives, inspect_archive, repack_archive
from qawrench.trace_budget.cli import main


def archive_end(raw, *, zip64=False, current=0, start=0, zip64_current=0,
                zip64_start=0, locator_disk=0, disks=1, comment=b"", prefix=b""):
    """Replace the classic end, optionally adding a forced ZIP64 end/locator.

    No writer/reader from production constructs these records. Member records
    and offsets are unchanged. A prefix exercises zipfile's concatenated ZIPs.
    """
    assert raw[-22:-18] == b"PK\x05\x06"
    _, _, _, here, total, size, offset, _ = struct.unpack("<4s4H2IH", raw[-22:])
    records = raw[:-22]
    if zip64:
        records += struct.pack("<4sQ2H2I4Q", b"PK\x06\x06", 44, 45, 45,
                               zip64_current, zip64_start, here, total, size, offset)
        records += struct.pack("<4sIQI", b"PK\x06\x07", locator_disk, len(raw) - 22, disks)
        here = total = 0xFFFF
        size = offset = 0xFFFFFFFF
    return prefix + records + struct.pack("<4s4H2IH", b"PK\x05\x06", current, start,
                                          here, total, size, offset, len(comment)) + comment


class ArchiveDiskTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / "source.zip"
        self.good = self.root / "good.zip"
        self.output = self.root / "output"
        self.good.write_bytes(synthetic_zip()[0])

    def cli(self, args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(list(map(str, args)))
        return code, stdout.getvalue(), stderr.getvalue()

    def preserved(self, raw):
        self.assertEqual(self.source.read_bytes(), raw)
        self.assertEqual(self.good.read_bytes(), synthetic_zip()[0])
        self.assertEqual(self.output.read_bytes(), b"existing report or archive")
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def reject(self, options, field, declared, supported=0):
        raw = archive_end(synthetic_zip()[0], **options)
        self.source.write_bytes(raw)
        self.output.write_bytes(b"existing report or archive")
        reason = re.escape(f"unsupported ZIP archive disk declaration: {str(self.source)!r}; "
                           f"{field} {declared}, supported {supported}")
        for operation in (lambda: inspect_archive(self.source),
                          lambda: compare_archives(self.source, self.good),
                          lambda: compare_archives(self.good, self.source),
                          lambda: repack_archive(self.source, self.output, force=True)):
            with self.assertRaisesRegex(ArchiveError, reason):
                operation()
            self.preserved(raw)
        for args in (["inspect", self.source], ["compare", self.source, self.good],
                     ["compare", self.good, self.source], ["repack", self.source],
                     ["repack", self.source, "--dry-run"]):
            with self.subTest(command=args[0], dry_run="--dry-run" in args):
                code, stdout, stderr = self.cli([*args, "--out", self.output, "--force"])
                self.assertEqual((code, stdout), (1, ""))
                self.assertRegex(stderr, "qawrench trace-budget: " + reason)
                self.preserved(raw)
        # Disk declarations fail before any payload decoding, even for an
        # otherwise entirely readable single-file archive.
        with patch("qawrench.trace_budget.archive._verified_chunks", side_effect=AssertionError("decoded payload")):
            with self.assertRaisesRegex(ArchiveError, reason):
                inspect_archive(self.source)
            with self.assertRaisesRegex(ArchiveError, reason):
                repack_archive(self.source, self.output, force=True)

    def test_confirmed_classic_current_disk_1(self):
        self.reject(dict(current=1), "classic EOCD current disk", 1)

    def test_both_classic_disk_fields_even_with_zip64_and_comments(self):
        for zip64 in (False, True):
            for key, field in (("current", "current disk"), ("start", "central-directory start disk")):
                for disk in (1, 2, 0xFFFF):
                    with self.subTest(zip64=zip64, key=key, disk=disk):
                        self.reject(dict(zip64=zip64, comment=b"synthetic comment", **{key: disk}),
                                    "classic EOCD " + field, disk)

    def test_zip64_end_and_locator_disk_requirements(self):
        for key, field, values, supported in (
            ("zip64_current", "ZIP64 EOCD current disk", (1, 0xFFFFFFFF), 0),
            ("zip64_start", "ZIP64 EOCD central-directory start disk", (1, 0xFFFFFFFF), 0),
            ("locator_disk", "ZIP64 locator end-record disk", (1, 0xFFFFFFFF), 0),
            ("disks", "ZIP64 locator total disks", (0, 2, 0xFFFFFFFF), 1)):
            for value in values:
                with self.subTest(key=key, value=value):
                    self.reject(dict(zip64=True, **{key: value}), field, value, supported)

    def test_invalid_completed_candidates_are_removed(self):
        raw, _ = synthetic_zip()
        self.source.write_bytes(raw)
        self.output.write_bytes(b"existing report or archive")
        for options in (dict(current=1), dict(start=1), dict(zip64=True, zip64_current=1),
                        dict(zip64=True, zip64_start=1), dict(zip64=True, locator_disk=1),
                        dict(zip64=True, disks=0)):
            candidates = []

            def corrupt_candidate(path, **kwargs):
                candidate = Path(path)
                self.assertTrue(candidate.is_file())
                self.assertTrue(candidate.name.endswith(".tmp"))
                # Readable, fully closed candidate exists before we alter its
                # end records and invoke the real inspection path.
                with zipfile.ZipFile(candidate) as z:
                    self.assertIsNone(z.testzip())
                candidates.append(candidate)
                candidate.write_bytes(archive_end(candidate.read_bytes(), **options))
                return inspect_archive(candidate, **kwargs)

            with self.subTest(options=options), patch("qawrench.trace_budget.archive.inspect_archive",
                                                     side_effect=corrupt_candidate):
                code, stdout, stderr = self.cli(["repack", self.source, "--out", self.output, "--force"])
                self.assertEqual((code, stdout), (1, ""))
                self.assertIn("unsupported ZIP archive disk declaration:", stderr)
                self.assertEqual(len(candidates), 1)
                self.assertIn(repr(str(candidates[0])), stderr)
                self.assertFalse(candidates[0].exists())
                self.preserved(raw)

    def test_single_disk_controls_preserve_reports_and_exact_payloads(self):
        for method in (0, 8):
            for member_zip64 in (False, True):
                for end_zip64 in (False, True):
                    for descriptor, signed in ((False, False), (True, False), (True, True)):
                        with self.subTest(method=method, member_zip64=member_zip64,
                                          end_zip64=end_zip64, descriptor=descriptor, signed=signed):
                            base, data = synthetic_zip(method=method, zip64=member_zip64,
                                central_zip64=member_zip64, descriptor=descriptor, signed=signed,
                                member_flags=0x800, body_name="resources/雪😀")
                            self.source.write_bytes(base)
                            expected = inspect_archive(self.source)
                            raw = archive_end(base, zip64=end_zip64)
                            self.source.write_bytes(raw)
                            overhead = len(raw) - len(base)
                            for key in ("archive_bytes", "zip_overhead_bytes"):
                                expected[key] += overhead
                            expected["budget"]["measured_bytes"] += overhead
                            self.assertEqual(inspect_archive(self.source), expected)
                            self.assertEqual({m["name"]:m["sha256"] for m in expected["members"]},
                                             {n:hashlib.sha256(p).hexdigest() for n,p in data.items()})
                            result = repack_archive(self.source, self.output, force=True)
                            self.assertTrue(result["all_member_hashes_equal"])
                            with zipfile.ZipFile(self.output) as z:
                                self.assertEqual({n:z.read(n) for n in z.namelist()}, data)
                            self.assertEqual(compare_archives(self.source, self.output), result["comparison"])
                            self.assertTrue(compare_archives(self.output, self.source)["all_retained_hashes_equal"])
                            dry = self.root / "dry.zip"
                            code, stdout, stderr = self.cli(["repack", self.source, "--out", dry, "--dry-run"])
                            self.assertEqual((code, stderr), (0, ""))
                            self.assertTrue(json.loads(stdout)["dry_run"])
                            self.assertFalse(dry.exists())
                            self.assertEqual(self.source.read_bytes(), raw)
                            self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_end_location_with_long_comments_and_prepended_data(self):
        base, _ = synthetic_zip()
        for zip64 in (False, True):
            for prefix in (b"", b"prepended data"):
                for comment in (b"", b"comment", b"x" * 65535):
                    with self.subTest(zip64=zip64, prefix=prefix, comment_length=len(comment)):
                        raw = archive_end(base, zip64=zip64, comment=comment, prefix=prefix)
                        self.source.write_bytes(raw)
                        self.assertTrue(inspect_archive(self.source)["verified"]["crc32"])
                        self.reject(dict(zip64=zip64, current=1, comment=comment, prefix=prefix),
                                    "classic EOCD current disk", 1)
                        if zip64:
                            self.reject(dict(zip64=True, zip64_current=1, comment=comment, prefix=prefix),
                                        "ZIP64 EOCD current disk", 1)


if __name__ == "__main__":
    unittest.main()
