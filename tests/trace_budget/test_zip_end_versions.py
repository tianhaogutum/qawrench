"""ZIP64 end extraction requirements, independent of member/made-by versions."""

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
from qawrench.trace_budget.archive import _verified_chunks
from qawrench.trace_budget.cli import main


def version_end(raw, *, needed=45, made_by=45, prefix=b"", comment=b""):
    """Write the raw +14 requirement; +12 is only the producer's version."""
    result = bytearray(archive_end(raw, zip64=True))
    struct.pack_into("<HH", result, len(result) - 98 + 12, made_by, needed)
    struct.pack_into("<H", result, len(result) - 2, len(comment))
    return prefix + result + comment


class EndVersionTests(unittest.TestCase):
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

    def reason(self, path, version):
        return (f"unsupported ZIP64 EOCD extraction-version declaration: {str(path)!r}; "
                f"required {version}, supported 45 (4.5)")

    def reject(self, version, **options):
        raw = version_end(self.base, needed=version, **options)
        self.source.write_bytes(raw)
        self.output.write_bytes(b"existing report or archive")
        # Payload validity is independently established; it cannot prove metadata validity.
        with zipfile.ZipFile(self.source) as z:
            self.assertEqual({n: z.read(n) for n in z.namelist()}, self.data)
        reason = re.escape(self.reason(self.source, version))
        operations = (lambda: inspect_archive(self.source),
                      lambda: compare_archives(self.source, self.good),
                      lambda: compare_archives(self.good, self.source),
                      lambda: repack_archive(self.source, self.output, force=True))
        decoded_invalid = []

        def guarded_chunks(archive, handle, *args):
            if Path(handle.name) == self.source:
                decoded_invalid.append(handle.name)
                raise AssertionError("decoded invalid archive")
            return _verified_chunks(archive, handle, *args)

        # Allows the valid comparison side to decode while forbidding the invalid
        # side in BOTH directions, and in every API/CLI path including dry-run.
        with patch("qawrench.trace_budget.archive._verified_chunks", side_effect=guarded_chunks):
            for operation in operations:
                with self.assertRaisesRegex(ArchiveError, reason):
                    operation()
                self.preserved(raw)
            for args in (["inspect", self.source], ["compare", self.source, self.good],
                         ["compare", self.good, self.source], ["repack", self.source],
                         ["repack", self.source, "--dry-run"]):
                code, stdout, stderr = self.cli([*args, "--out", self.output, "--force"])
                self.assertEqual((code, stdout), (1, ""))
                self.assertEqual(stderr, "qawrench trace-budget: " + self.reason(self.source, version) + "\n")
                self.preserved(raw)
        self.assertEqual(decoded_invalid, [])

    def test_confirmed_zip64_end_requirement_99(self):
        self.reject(99)

    def test_low_high_and_nonzero_high_byte_requirements(self):
        for version in (0, 10, 20, 44, 46, 63, 255, 0x0100, 0x012d, 0x032d, 0xffff):
            with self.subTest(version=version):
                self.reject(version)

    def test_requirement_with_comments_and_prepended_data(self):
        for prefix in (b"", b"prepended data"):
            for comment in (b"", b"comment", b"x" * 65535):
                with self.subTest(prefix=prefix, comment_length=len(comment)):
                    self.source.write_bytes(version_end(self.base, prefix=prefix, comment=comment))
                    self.assertEqual(inspect_archive(self.source)["member_count"], 3)
                    self.reject(44, prefix=prefix, comment=comment)

    def test_invalid_completed_candidates_removed(self):
        self.source.write_bytes(self.base)
        self.output.write_bytes(b"existing report or archive")
        for version in (0, 44, 46, 99, 0x032d, 0xffff):
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
                    candidate.write_bytes(version_end(candidate.read_bytes(), needed=version))
                    with patch("qawrench.trace_budget.archive._verified_chunks",
                               side_effect=AssertionError("decoded invalid candidate")):
                        return inspect_archive(candidate, **kwargs)

                with self.subTest(version=version, force=force), patch(
                        "qawrench.trace_budget.archive.inspect_archive", side_effect=corrupt_candidate):
                    code, stdout, stderr = self.cli(["repack", self.source, "--out", target,
                                                     *(["--force"] if force else [])])
                    self.assertEqual((code, stdout), (1, ""))
                    self.assertEqual(len(candidates), 1)
                    self.assertEqual(stderr, "qawrench trace-budget: " + self.reason(candidates[0], version) + "\n")
                    self.assertFalse(candidates[0].exists())
                    self.preserved(self.base)
                    if not force:
                        self.assertFalse(target.exists())

    def test_made_by_is_independent_of_extraction_requirement(self):
        self.source.write_bytes(version_end(self.base))
        expected = inspect_archive(self.source)
        for made_by in (0, 20, 45, 99, 0x032d, 0xffff):
            with self.subTest(made_by=made_by):
                self.source.write_bytes(version_end(self.base, made_by=made_by))
                self.assertEqual(inspect_archive(self.source), expected)
        # Member requirements stay at 20 even though the end requires 45.
        with zipfile.ZipFile(self.source) as z:
            self.assertEqual({i.extract_version for i in z.infolist()}, {20})

    def test_supported_end_versions_keep_reports_and_every_payload(self):
        for method in (0, 8):
            for member_zip64 in (False, True):
                for descriptor, signed in ((False, False), (True, False), (True, True)):
                    base, data = synthetic_zip(method=method, zip64=member_zip64,
                        central_zip64=member_zip64, descriptor=descriptor, signed=signed,
                        member_flags=0x800, body_name="resources/雪😀")
                    self.source.write_bytes(base)
                    expected = inspect_archive(self.source)
                    for end_zip64 in (False, True):
                        with self.subTest(method=method, member_zip64=member_zip64,
                                          descriptor=descriptor, signed=signed, end_zip64=end_zip64):
                            raw = version_end(base, made_by=0x032d) if end_zip64 else base
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


if __name__ == "__main__":
    unittest.main()
