"""Selected classic EOCD comment extents, also when ZIP64 ends are present."""

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
from .test_zip_end_directory import directory_end
from qawrench.trace_budget import ArchiveError, compare_archives, inspect_archive, repack_archive
from qawrench.trace_budget.archive import _verified_chunks
from qawrench.trace_budget.cli import main


def comment_end(raw, *, declared=None, comment=b"", trailing=b"", zip64=False,
                prefix=b"", prefix_offsets=False):
    """Encode comment bytes/declaration independently of production parsing."""
    result = bytearray(directory_end(raw, zip64=zip64, classic=((0xFFFFFFFF, 0xFFFFFFFF) if zip64 else (None, None)),
                                    prefix=prefix, prefix_offsets=prefix_offsets))
    struct.pack_into("<H", result, len(result) - 2, len(comment) if declared is None else declared)
    return bytes(result) + comment + trailing


def truncated_comment(path, declared, available):
    return (f"truncated ZIP archive comment: {str(path)!r}; "
            f"classic EOCD declared {declared} bytes, available {available} bytes")


class EndCommentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / "source.zip"
        self.good = self.root / "good.zip"
        self.output = self.root / "output"
        self.base, self.data = synthetic_zip()
        self.good.write_bytes(self.base)
        self.output.write_bytes(b"existing report or archive")

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

    def test_confirmed_classic_truncations(self):
        self.confirmed(False)

    def test_confirmed_zip64_truncations(self):
        self.confirmed(True)

    def confirmed(self, zip64):
        for declared, comment in ((1, b""), (65535, b"partial")):
            with self.subTest(zip64=zip64, declared=declared):
                raw = comment_end(self.base, zip64=zip64, declared=declared, comment=comment)
                self.source.write_bytes(raw)
                # Independent stdlib decode proves that every payload is valid;
                # the missing rejection is the archive comment boundary only.
                with zipfile.ZipFile(self.source) as z:
                    self.assertEqual({n: z.read(n) for n in z.namelist()}, self.data)
                with self.assertRaisesRegex(ArchiveError, re.escape(
                        truncated_comment(self.source, declared, len(comment)))):
                    inspect_archive(self.source)

    def test_truncated_comments_reject_all_operations_before_decoding(self):
        for zip64 in (False, True):
            for prefix, physical in ((b"", False), (b"prepended data", False), (b"prepended data", True)):
                for declared, comment in ((1, b""), (8, b"partial"), (65535, b""),
                                          (65535, b"partial"), (65535, b"x" * 65534)):
                    with self.subTest(zip64=zip64, prefix=prefix, physical=physical, declared=declared,
                                      available=len(comment)):
                        raw = comment_end(self.base, zip64=zip64, prefix=prefix, prefix_offsets=physical,
                                          declared=declared, comment=comment)
                        self.source.write_bytes(raw)
                        reason = truncated_comment(self.source, declared, len(comment))

                        def guard(archive, handle, *args):
                            self.assertNotEqual(Path(handle.name), self.source, "decoded invalid archive")
                            return _verified_chunks(archive, handle, *args)

                        with patch("qawrench.trace_budget.archive._verified_chunks", side_effect=guard):
                            for operation in (lambda: inspect_archive(self.source),
                                              lambda: compare_archives(self.source, self.good),
                                              lambda: compare_archives(self.good, self.source),
                                              lambda: repack_archive(self.source, self.output, force=True)):
                                with self.assertRaisesRegex(ArchiveError, re.escape(reason)):
                                    operation()
                                self.preserved(raw)
                            for args in (["inspect", self.source], ["compare", self.source, self.good],
                                         ["compare", self.good, self.source], ["repack", self.source],
                                         ["repack", self.source, "--dry-run"]):
                                for force in (False, True):
                                    target = self.output if force else self.root / "absent"
                                    code, stdout, stderr = self.cli([*args, "--out", target,
                                                                     *(["--force"] if force else [])])
                                    self.assertEqual((code, stdout, stderr),
                                                     (1, "", "qawrench trace-budget: " + reason + "\n"))
                                    if not force:
                                        self.assertFalse(target.exists())
                                    self.preserved(raw)

    def test_selected_truncated_record_does_not_fall_back_to_earlier_end(self):
        selected = bytearray(self.base[-22:])
        struct.pack_into("<H", selected, 20, 1)
        self.source.write_bytes(self.base + b"padding" + selected)
        reason = truncated_comment(self.source, 1, 0)
        with patch("qawrench.trace_budget.archive.zipfile.ZipFile",
                   side_effect=AssertionError("parsed directory before comment check")):
            with self.assertRaisesRegex(ArchiveError, re.escape(reason)):
                inspect_archive(self.source)

    def test_invalid_completed_candidates_removed(self):
        self.source.write_bytes(self.base)
        for zip64 in (False, True):
            for declared, comment in ((1, b""), (8, b"partial"), (65535, b"x" * 65534)):
                for force in (False, True):
                    candidates = []
                    target = self.output if force else self.root / "absent.zip"

                    def corrupt_candidate(path, **kwargs):
                        candidate = Path(path)
                        self.assertTrue(candidate.is_file())
                        self.assertTrue(candidate.name.endswith(".tmp"))
                        with zipfile.ZipFile(candidate) as z:
                            self.assertEqual({n: z.read(n) for n in z.namelist()}, self.data)
                        candidates.append(candidate)
                        candidate.write_bytes(comment_end(candidate.read_bytes(), zip64=zip64,
                            declared=declared, comment=comment, prefix=b"prepended data"))
                        self.candidate_reason = truncated_comment(candidate, declared, len(comment))
                        with patch("qawrench.trace_budget.archive._verified_chunks",
                                   side_effect=AssertionError("decoded invalid candidate")):
                            return inspect_archive(candidate, **kwargs)

                    with self.subTest(zip64=zip64, declared=declared, force=force), patch(
                            "qawrench.trace_budget.archive.inspect_archive", side_effect=corrupt_candidate):
                        code, stdout, stderr = self.cli(["repack", self.source, "--out", target,
                                                        *(["--force"] if force else [])])
                        self.assertEqual((code, stdout, stderr),
                                         (1, "", "qawrench trace-budget: " + self.candidate_reason + "\n"))
                        self.assertEqual(len(candidates), 1)
                        self.assertFalse(candidates[0].exists())
                        self.preserved(self.base)
                        if not force:
                            self.assertFalse(target.exists())

    def test_fully_present_comments_and_trailing_bytes_preserve_reports_and_payloads(self):
        for method in (0, 8):
            for member_zip64 in (False, True):
                for descriptor, signed in ((False, False), (True, False), (True, True)):
                    base, data = synthetic_zip(method=method, zip64=member_zip64,
                        central_zip64=member_zip64, descriptor=descriptor, signed=signed,
                        member_flags=0x800, body_name="resources/雪😀")
                    for end_zip64 in (False, True):
                        for comment, trailing in ((b"", b""), (b"comment", b""), (b"x" * 65535, b""),
                                                   (b"", b"trailing data"), (b"comment", b"trailing data")):
                            with self.subTest(method=method, member_zip64=member_zip64, descriptor=descriptor,
                                             signed=signed, end_zip64=end_zip64, comment=len(comment), trailing=trailing):
                                self.source.write_bytes(base)
                                expected = inspect_archive(self.source)
                                raw = comment_end(base, zip64=end_zip64, comment=comment, trailing=trailing,
                                                  prefix=b"prepended data", prefix_offsets=member_zip64)
                                self.source.write_bytes(raw)
                                for key in ("archive_bytes", "zip_overhead_bytes"):
                                    expected[key] += len(raw) - len(base)
                                expected["budget"]["measured_bytes"] += len(raw) - len(base)
                                self.assertEqual(inspect_archive(self.source), expected)
                                result = repack_archive(self.source, self.output, force=True)
                                with zipfile.ZipFile(self.source) as z:
                                    self.assertEqual({n: z.read(n) for n in z.namelist()}, data)
                                with zipfile.ZipFile(self.output) as z:
                                    self.assertEqual({n: z.read(n) for n in z.namelist()}, data)
                                self.assertEqual(compare_archives(self.source, self.output), result["comparison"])
                                self.assertTrue(compare_archives(self.output, self.source)["all_retained_hashes_equal"])
                                dry = self.root / "dry.zip"
                                code, stdout, stderr = self.cli(["repack", self.source, "--out", dry, "--dry-run"])
                                self.assertEqual((code, stderr), (0, ""))
                                self.assertTrue(json.loads(stdout)["dry_run"])
                                self.assertFalse(dry.exists())
                                self.assertEqual(self.source.read_bytes(), raw)
                                self.assertEqual(list(self.root.glob(".*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
