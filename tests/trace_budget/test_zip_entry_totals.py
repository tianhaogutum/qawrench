"""Resolved end totals must count directory records, independently of payloads."""

from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from .test_zip_headers import synthetic_zip
from .test_zip_end_counts import count_end
from qawrench.trace_budget import ArchiveError, compare_archives, inspect_archive, repack_archive
from qawrench.trace_budget.archive import _verified_chunks
from qawrench.trace_budget.cli import main


def total_end(raw, total, *, zip64=False, classic=None, **kwargs):
    return count_end(raw, zip64=zip64,
                     classic=classic if classic is not None else ((65535, 65535) if zip64 else (total, total)),
                     counts=(total, total) if zip64 else None, **kwargs)


class EntryTotalTests(unittest.TestCase):
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

    def reject(self, total, **options):
        raw = total_end(self.base, total, **options)
        self.source.write_bytes(raw)
        reason = re.escape(f"ZIP central-directory entry count mismatch: {str(self.source)!r}; "
                           f"declared {total}, actual 3")
        # Only the valid comparison input may decode. Metadata proof must not
        # depend on payload CRC/hash success, even for the after-side input.
        def guard(archive, handle, *args, **kwargs):
            self.assertNotEqual(Path(handle.name), self.source, "decoded invalid archive")
            return _verified_chunks(archive, handle, *args, **kwargs)

        with patch("qawrench.trace_budget.archive._verified_chunks", side_effect=guard):
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
                code, stdout, stderr = self.cli([*args, "--out", self.output, "--force"])
                self.assertEqual((code, stdout), (1, ""))
                self.assertRegex(stderr, "qawrench trace-budget: " + reason)
                self.preserved(raw)
            absent = self.root / "absent.zip"
            code, stdout, stderr = self.cli(["repack", self.source, "--out", absent])
            self.assertEqual((code, stdout), (1, ""))
            self.assertRegex(stderr, reason)
            self.assertFalse(absent.exists())

    def test_confirmed_classic_and_zip64_underdeclared_totals(self):
        for zip64 in (False, True):
            with self.subTest(zip64=zip64):
                self.reject(2, zip64=zip64)

    def test_zero_lower_higher_and_64_bit_totals(self):
        for total in (0, 1, 4, 65534):
            with self.subTest(total=total):
                self.reject(total)
        for total in (0, 1, 4, 65535, 2**32, 2**64 - 1):
            for classic in ((65535, 65535), *(((total, total), (total, 65535), (65535, total))
                                            if total < 65535 else ())):
                with self.subTest(total=total, classic=classic):
                    self.reject(total, zip64=True, classic=classic)

    def test_comments_and_prepended_data_do_not_hide_mismatches(self):
        for zip64 in (False, True):
            for prefix in (b"", b"prepended data"):
                for comment in (b"", b"comment", b"x" * 65535):
                    with self.subTest(zip64=zip64, prefix=prefix, comment_length=len(comment)):
                        self.reject(2, zip64=zip64, prefix=prefix, comment=comment)

    def test_invalid_completed_candidates_are_removed(self):
        self.source.write_bytes(self.base)
        for zip64, total in ((False, 0), (False, 2), (False, 4),
                             (True, 0), (True, 2), (True, 4)):
            for force in (False, True):
                target = self.output if force else self.root / "absent.zip"
                candidates = []

                def corrupt_candidate(path, **kwargs):
                    candidate = Path(path)
                    self.assertTrue(candidate.is_file())
                    self.assertTrue(candidate.name.endswith(".tmp"))
                    candidates.append(candidate)
                    candidate.write_bytes(total_end(candidate.read_bytes(), total, zip64=zip64))
                    with patch("qawrench.trace_budget.archive._verified_chunks",
                               side_effect=AssertionError("decoded invalid candidate")):
                        return inspect_archive(candidate, **kwargs)

                with self.subTest(zip64=zip64, total=total, force=force), patch(
                        "qawrench.trace_budget.archive.inspect_archive", side_effect=corrupt_candidate):
                    code, stdout, stderr = self.cli(["repack", self.source, "--out", target,
                                                     *(["--force"] if force else [])])
                    self.assertEqual((code, stdout), (1, ""))
                    self.assertEqual(len(candidates), 1)
                    self.assertIn(f"ZIP central-directory entry count mismatch: {str(candidates[0])!r}; "
                                  f"declared {total}, actual 3", stderr)
                    self.assertFalse(candidates[0].exists())
                    self.preserved(self.base)
                    if not force:
                        self.assertFalse(target.exists())

    def test_empty_and_directory_entries_count_without_payload_decoding(self):
        for data in ({}, {"0.trace": self.data["0.trace"], "empty/": b"", "empty/file": b""}):
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w") as z:
                for name, payload in data.items():
                    z.writestr(name, payload)
            base = stream.getvalue()
            for zip64 in (False, True):
                self.source.write_bytes(total_end(base, len(data), zip64=zip64))
                if data:
                    self.assertEqual(inspect_archive(self.source)["member_count"], 3)
                else:
                    with self.assertRaisesRegex(ArchiveError, "no .trace stream"):
                        inspect_archive(self.source)
                self.source.write_bytes(total_end(base, len(data) + 1, zip64=zip64))
                with patch("qawrench.trace_budget.archive._verified_chunks", side_effect=AssertionError("decoded payload")):
                    with self.assertRaisesRegex(ArchiveError, f"declared {len(data) + 1}, actual {len(data)}"):
                        inspect_archive(self.source)


if __name__ == "__main__":
    unittest.main()
