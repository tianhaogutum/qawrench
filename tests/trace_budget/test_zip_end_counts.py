"""Single-disk count contracts using independently encoded raw end records."""

from contextlib import redirect_stderr, redirect_stdout
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
from .test_zip_end_disks import archive_end
from qawrench.trace_budget import ArchiveError, compare_archives, inspect_archive, repack_archive
from qawrench.trace_budget.cli import main


def count_end(raw, *, zip64=False, classic=None, counts=None, prefix=b"", comment=b""):
    """Change end counts, never member records or payloads, without production code."""
    result = bytearray(archive_end(raw, zip64=zip64))
    if classic is not None:
        struct.pack_into("<HH", result, len(result) - 22 + 8, *classic)
    if counts is not None:
        assert zip64
        struct.pack_into("<QQ", result, len(result) - 98 + 24, *counts)
    struct.pack_into("<H", result, len(result) - 2, len(comment))
    return prefix + result + comment


class EndCountTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / "source.zip"
        self.good = self.root / "good.zip"
        self.output = self.root / "output"
        self.base, self.data = synthetic_zip()
        self.good.write_bytes(self.base)

    def cli(self, args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(list(map(str, args)))
        return code, stdout.getvalue(), stderr.getvalue()

    def preserved(self, raw):
        self.assertEqual(self.source.read_bytes(), raw)
        self.assertEqual(self.good.read_bytes(), self.base)
        self.assertEqual(self.output.read_bytes(), b"existing report or archive")
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def reject(self, options, details):
        raw = count_end(self.base, **options)
        self.source.write_bytes(raw)
        self.output.write_bytes(b"existing report or archive")
        # Independently show payloads remain valid; decoding cannot validate counts.
        with zipfile.ZipFile(self.source) as z:
            self.assertEqual({n: z.read(n) for n in z.namelist()}, self.data)
        reason = re.escape(f"inconsistent ZIP end-record entry counts: {str(self.source)!r}; " + details)
        operations = (lambda: inspect_archive(self.source),
                      lambda: compare_archives(self.source, self.good),
                      lambda: compare_archives(self.good, self.source),
                      lambda: repack_archive(self.source, self.output, force=True))
        for operation in operations:
            with self.assertRaisesRegex(ArchiveError, reason):
                operation()
            self.preserved(raw)
        for args in (["inspect", self.source], ["compare", self.source, self.good],
                     ["compare", self.good, self.source], ["repack", self.source],
                     ["repack", self.source, "--dry-run"]):
            code, stdout, stderr = self.cli([*args, "--out", self.output, "--force"])
            self.assertEqual((code, stdout), (1, ""))
            self.assertRegex(stderr, "qawrench trace-budget: " + reason)
            self.preserved(raw)
        # Valid before-side scans may decode; the invalid archive itself must not.
        with patch("qawrench.trace_budget.archive._verified_chunks", side_effect=AssertionError("decoded payload")):
            for operation in (operations[0], operations[1], operations[3]):
                with self.assertRaisesRegex(ArchiveError, reason):
                    operation()
            code, stdout, stderr = self.cli(["repack", self.source, "--out", self.output,
                                             "--force", "--dry-run"])
            self.assertEqual((code, stdout), (1, ""))
            self.assertRegex(stderr, reason)

    def test_confirmed_classic_and_zip64_mismatches(self):
        for zip64 in (False, True):
            with self.subTest(zip64=zip64):
                self.reject(dict(zip64=zip64, **({"counts": (0, 3)} if zip64 else {"classic": (0, 3)})),
                            f"{'ZIP64' if zip64 else 'classic'} EOCD entries on this disk 0, total entries 3")

    def test_count_directions_and_64_bit_values(self):
        for here, total in ((3, 0), (2, 3), (4, 3), (65535, 3), (2**32, 3), (3, 2**64 - 1)):
            with self.subTest(here=here, total=total):
                self.reject(dict(zip64=True, counts=(here, total)),
                            f"ZIP64 EOCD entries on this disk {here}, total entries {total}")
        self.reject(dict(classic=(3, 0)), "classic EOCD entries on this disk 3, total entries 0")
        # ZIP64 must not mask contradictory classic literal declarations.
        self.reject(dict(zip64=True, classic=(0, 3)),
                    "classic EOCD entries on this disk 0, ZIP64 EOCD entries on this disk 3")
        self.reject(dict(zip64=True, classic=(3, 0)),
                    "classic EOCD total entries 0, ZIP64 EOCD total entries 3")
        self.reject(dict(zip64=True, classic=(2, 2)),
                    "classic EOCD entries on this disk 2, ZIP64 EOCD entries on this disk 3")

    def test_classic_sentinel_policy(self):
        for classic in ((65535, 3), (3, 65535), (65535, 65535)):
            with self.subTest(classic=classic):
                self.reject(dict(classic=classic),
                            "classic EOCD count sentinel 65535 requires a readable ZIP64 EOCD")
        for classic, detail in (((65535, 2), "total entries 2, ZIP64 EOCD total entries 3"),
                                ((2, 65535), "entries on this disk 2, ZIP64 EOCD entries on this disk 3")):
            with self.subTest(classic=classic):
                self.reject(dict(zip64=True, classic=classic), "classic EOCD " + detail)

    def test_invalid_completed_candidates_are_removed(self):
        self.source.write_bytes(self.base)
        self.output.write_bytes(b"existing report or archive")
        for options in (dict(classic=(0, 3)), dict(zip64=True, counts=(0, 3)),
                        dict(zip64=True, classic=(0, 3)), dict(classic=(65535, 65535))):
            for force in (False, True):
                target = self.output if force else self.root / "absent.zip"
                candidates = []

                def corrupt_candidate(path, **kwargs):
                    candidate = Path(path)
                    self.assertTrue(candidate.is_file())
                    self.assertTrue(candidate.name.endswith(".tmp"))
                    with zipfile.ZipFile(candidate) as z:
                        self.assertEqual({n: z.read(n) for n in z.namelist()}, self.data)
                    candidates.append(candidate)
                    candidate.write_bytes(count_end(candidate.read_bytes(), **options))
                    # Real inspection must reject metadata before decoding.
                    with patch("qawrench.trace_budget.archive._verified_chunks",
                               side_effect=AssertionError("decoded invalid candidate")):
                        return inspect_archive(candidate, **kwargs)

                with self.subTest(options=options, force=force), patch(
                        "qawrench.trace_budget.archive.inspect_archive", side_effect=corrupt_candidate):
                    code, stdout, stderr = self.cli(["repack", self.source, "--out", target,
                                                     *(["--force"] if force else [])])
                    self.assertEqual((code, stdout), (1, ""))
                    self.assertIn("inconsistent ZIP end-record entry counts:", stderr)
                    self.assertEqual(len(candidates), 1)
                    self.assertIn(repr(str(candidates[0])), stderr)
                    self.assertFalse(candidates[0].exists())
                    self.preserved(self.base)
                    if not force:
                        self.assertFalse(target.exists())

    def test_equal_counts_and_classic_zip64_sentinels_keep_exact_reports(self):
        for method in (0, 8):
            for member_zip64 in (False, True):
                for descriptor, signed in ((False, False), (True, False), (True, True)):
                    base, data = synthetic_zip(method=method, zip64=member_zip64,
                        central_zip64=member_zip64, descriptor=descriptor, signed=signed,
                        member_flags=0x800, body_name="resources/雪😀")
                    self.source.write_bytes(base)
                    expected = inspect_archive(self.source)
                    for zip64, classic in ((False, (3, 3)), (True, (3, 3)),
                                          (True, (65535, 65535)), (True, (3, 65535)), (True, (65535, 3))):
                        with self.subTest(method=method, member_zip64=member_zip64,
                                          descriptor=descriptor, signed=signed, zip64=zip64, classic=classic):
                            raw = count_end(base, zip64=zip64, classic=classic)
                            self.source.write_bytes(raw)
                            report = dict(expected)
                            overhead = len(raw) - len(base)
                            for key in ("archive_bytes", "zip_overhead_bytes"):
                                report[key] += overhead
                            report["budget"] = dict(expected["budget"])
                            report["budget"]["measured_bytes"] += overhead
                            self.assertEqual(inspect_archive(self.source), report)
                            result = repack_archive(self.source, self.output, force=True)
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

    def test_count_checks_with_comments_and_prepended_data(self):
        for zip64 in (False, True):
            for prefix in (b"", b"prepended data"):
                for comment in (b"", b"comment", b"x" * 65535):
                    with self.subTest(zip64=zip64, prefix=prefix, comment_length=len(comment)):
                        self.source.write_bytes(count_end(self.base, zip64=zip64, prefix=prefix, comment=comment))
                        self.assertEqual(inspect_archive(self.source)["member_count"], 3)
                        self.reject(dict(zip64=zip64, prefix=prefix, comment=comment,
                                         **({"counts": (0, 3)} if zip64 else {"classic": (0, 3)})),
                                    f"{'ZIP64' if zip64 else 'classic'} EOCD entries on this disk 0, total entries 3")


if __name__ == "__main__":
    unittest.main()
