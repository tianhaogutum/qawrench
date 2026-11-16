"""Single-central-ZIP64 compatibility policy, separate from framing/lengths."""

from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from .test_zip_headers import synthetic_zip
from .test_zip_end_comments import comment_end
from .test_zip_central_extra import central_extra, UNKNOWN
from qawrench.trace_budget import ArchiveError, compare_archives, inspect_archive, repack_archive
from qawrench.trace_budget.archive import _verified_chunks
from qawrench.trace_budget.cli import main


def duplicate_extra(raw, *, conflicting=False, separated=False, member="resources/body"):
    """Add two complete central size blocks, without changing any local byte."""
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        info = archive.getinfo(member)
    first = struct.pack("<HHQQ", 1, 16, info.file_size, info.compress_size)
    second = struct.pack("<HHQQ", 1, 16, 1 if conflicting else info.file_size, info.compress_size)
    return central_extra(raw, first + (UNKNOWN if separated else b"") + second,
                         member=member, fields=((24, 0xFFFFFFFF), (20, 0xFFFFFFFF)))


def duplicate_reason(path, member="resources/body"):
    return f"invalid ZIP central ZIP64: {str(path)!r}; {member!r}; duplicate block"


class CentralZip64Tests(unittest.TestCase):
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

    def confirmed(self, conflicting):
        base, data = synthetic_zip(zip64=True, central_zip64=True)
        for separated in (False, True):
            raw = duplicate_extra(base, conflicting=conflicting, separated=separated)
            self.source.write_bytes(raw)
            # The independent reader confirms that framing and payloads are valid.
            with zipfile.ZipFile(self.source) as archive:
                self.assertEqual({n: archive.read(n) for n in archive.namelist()}, data)
            with self.subTest(separated=separated):
                with self.assertRaises(ArchiveError) as caught:
                    inspect_archive(self.source)
                self.assertEqual(str(caught.exception), duplicate_reason(self.source))

    def test_confirmed_identical_duplicates(self):
        self.confirmed(False)

    def test_confirmed_conflicting_duplicates(self):
        self.confirmed(True)

    def test_all_operations_reject_before_any_invalid_archive_decoding(self):
        for method in (0, 8):
            for local64 in (False, True):
                for descriptor, signed in ((False, False), (True, False), (True, True)):
                    name = "resources/雪😀"
                    base, data = synthetic_zip(method=method, zip64=local64, central_zip64=local64,
                        descriptor=descriptor, signed=signed, body_name=name, member_flags=0x800,
                        local_version=45, central_version=45)
                    for conflicting in (False, True):
                        for separated in (False, True):
                            raw = duplicate_extra(base, conflicting=conflicting, separated=separated, member=name)
                            raw = comment_end(raw, zip64=local64, prefix=b"prepended data", comment=b"comment")
                            self.source.write_bytes(raw)
                            reason = duplicate_reason(self.source, name)
                            with zipfile.ZipFile(self.source) as archive:
                                self.assertEqual({n: archive.read(n) for n in archive.namelist()}, data)

                            def guard(archive, handle, *args):
                                self.assertNotEqual(Path(handle.name), self.source, "decoded invalid archive")
                                return _verified_chunks(archive, handle, *args)

                            with self.subTest(method=method, local64=local64, descriptor=descriptor,
                                              signed=signed, conflicting=conflicting, separated=separated), patch(
                                    "qawrench.trace_budget.archive._verified_chunks", side_effect=guard):
                                for operation in (lambda: inspect_archive(self.source),
                                                  lambda: compare_archives(self.source, self.good),
                                                  lambda: compare_archives(self.good, self.source),
                                                  lambda: repack_archive(self.source, self.output, force=True)):
                                    with self.assertRaises(ArchiveError) as caught:
                                        operation()
                                    self.assertEqual(str(caught.exception), reason)
                                    self.preserved(raw)
                                for args in (["inspect", self.source], ["compare", self.source, self.good],
                                             ["compare", self.good, self.source], ["repack", self.source],
                                             ["repack", self.source, "--dry-run"]):
                                    for force in (False, True):
                                        target = self.output if force else self.root / "absent"
                                        self.assertEqual(self.cli([*args, "--out", target, *(["--force"] if force else [])]),
                                                         (1, "", "qawrench trace-budget: " + reason + "\n"))
                                        if not force:
                                            self.assertFalse(target.exists())
                                        self.preserved(raw)

    def test_invalid_completed_candidates_are_physically_removed(self):
        self.source.write_bytes(self.base)
        for conflicting in (False, True):
            for separated in (False, True):
                for force in (False, True):
                    candidates, reasons = [], []
                    target = self.output if force else self.root / "absent.zip"

                    def corrupt_candidate(path, **kwargs):
                        candidate = Path(path)
                        self.assertTrue(candidate.is_file())
                        self.assertTrue(candidate.name.endswith(".tmp"))
                        with zipfile.ZipFile(candidate) as archive:
                            self.assertEqual({n: archive.read(n) for n in archive.namelist()}, self.data)
                        candidates.append(candidate)
                        candidate.write_bytes(duplicate_extra(candidate.read_bytes(), conflicting=conflicting, separated=separated))
                        reasons.append(duplicate_reason(candidate))
                        with patch("qawrench.trace_budget.archive._verified_chunks", side_effect=AssertionError("decoded invalid candidate")):
                            return inspect_archive(candidate, **kwargs)

                    with self.subTest(conflicting=conflicting, separated=separated, force=force), patch(
                            "qawrench.trace_budget.archive.inspect_archive", side_effect=corrupt_candidate):
                        result = self.cli(["repack", self.source, "--out", target, *(["--force"] if force else [])])
                        self.assertEqual(result, (1, "", "qawrench trace-budget: " + reasons[0] + "\n"))
                        self.assertEqual(len(candidates), 1)
                        self.assertFalse(candidates[0].exists())
                        self.preserved(self.base)
                        if not force:
                            self.assertFalse(target.exists())

    def test_duplicate_empty_blocks_without_sentinels_are_also_rejected(self):
        raw, _ = synthetic_zip(local_version=45, central_version=45)
        for separated in (False, True):
            self.source.write_bytes(central_extra(raw, bytes.fromhex("01000000") +
                (UNKNOWN if separated else b"") + bytes.fromhex("01000000")))
            with patch("qawrench.trace_budget.archive._verified_chunks", side_effect=AssertionError("decoded invalid archive")):
                with self.assertRaises(ArchiveError) as caught:
                    inspect_archive(self.source)
            self.assertEqual(str(caught.exception), duplicate_reason(self.source))

    def test_single_block_surplus_data_policy_is_unchanged(self):
        raw, _ = synthetic_zip(local_version=45, central_version=45)
        for length in (9, 15, 17):
            extra = struct.pack("<HHQ", 1, length, len(self.data["resources/body"])) + b"x" * (length - 8)
            self.source.write_bytes(central_extra(raw, extra, fields=((24, 0xFFFFFFFF),)))
            self.assertEqual(inspect_archive(self.source)["member_count"], 3)


if __name__ == "__main__":
    unittest.main()
