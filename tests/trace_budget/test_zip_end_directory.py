"""Raw classic/ZIP64 central-directory size and offset agreement contracts."""

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


SENTINEL = 0xFFFFFFFF


def directory_end(raw, *, zip64=True, classic=(SENTINEL, SENTINEL), resolved=None,
                  prefix=b"", comment=b"", prefix_offsets=False):
    """Encode redundant fields without production helpers; None means matching literal.

    By default offsets exclude the prefix (concatenated ZIP). prefix_offsets=True
    makes ALL offsets include it, so both supported coordinate systems are tested.
    """
    size, offset = struct.unpack_from("<II", raw, len(raw) - 10)
    if prefix_offsets:
        result = bytearray(raw)
        pos = offset
        while pos < offset + size:
            assert result[pos:pos + 4] == b"PK\x01\x02"
            local = struct.unpack_from("<I", result, pos + 42)[0]
            struct.pack_into("<I", result, pos + 42, local + len(prefix))
            pos += 46 + sum(struct.unpack_from("<HHH", result, pos + 28))
        offset += len(prefix)
        struct.pack_into("<I", result, len(result) - 6, offset)
        raw = bytes(result)
    result = bytearray(archive_end(raw, zip64=zip64))
    actual = (size, offset)
    struct.pack_into("<II", result, len(result) - 10,
                     *(a if c is None else c for c, a in zip(classic, actual)))
    if zip64:
        struct.pack_into("<QQ", result, len(result) - 98 + 40, *(resolved or actual))
        if prefix_offsets:
            struct.pack_into("<Q", result, len(result) - 42 + 8, len(raw) - 22 + len(prefix))
    struct.pack_into("<H", result, len(result) - 2, len(comment))
    return prefix + result + comment


def mismatch(path, field, classic, resolved):
    return (f"inconsistent ZIP central-directory declarations: {str(path)!r}; "
            f"classic EOCD central-directory {field} {classic}, "
            f"ZIP64 EOCD central-directory {field} {resolved}")


class EndDirectoryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / "source.zip"
        self.good = self.root / "good.zip"
        self.output = self.root / "output"
        self.base, self.data = synthetic_zip()
        self.size, self.offset = struct.unpack_from("<II", self.base, len(self.base) - 10)
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

    def test_confirmed_classic_size_zero(self):
        self.source.write_bytes(directory_end(self.base, classic=(0, SENTINEL)))
        with self.assertRaisesRegex(ArchiveError, re.escape(mismatch(self.source, "size", 0, self.size))):
            inspect_archive(self.source)

    def test_confirmed_classic_offset_zero(self):
        self.source.write_bytes(directory_end(self.base, classic=(SENTINEL, 0)))
        with self.assertRaisesRegex(ArchiveError, re.escape(mismatch(self.source, "offset", 0, self.offset))):
            inspect_archive(self.source)

    def reject(self, options, details, readable=True):
        raw = directory_end(self.base, **options)
        self.source.write_bytes(raw)
        if readable:
            with zipfile.ZipFile(self.source) as z:
                self.assertEqual({n: z.read(n) for n in z.namelist()}, self.data)
        reason = f"inconsistent ZIP central-directory declarations: {str(self.source)!r}; {details}"

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
                code, stdout, stderr = self.cli([*args, "--out", self.output, "--force"])
                self.assertEqual((code, stdout, stderr), (1, "", "qawrench trace-budget: " + reason + "\n"))
                self.preserved(raw)
            absent = self.root / "absent.zip"
            code, stdout, stderr = self.cli(["repack", self.source, "--out", absent])
            self.assertEqual((code, stdout, stderr), (1, "", "qawrench trace-budget: " + reason + "\n"))
            self.assertFalse(absent.exists())
            self.preserved(raw)

    def test_lower_higher_independent_and_combined_literals(self):
        for index, field, actual in ((0, "size", self.size), (1, "offset", self.offset)):
            for value in (0, actual - 1, actual + 1, SENTINEL - 1):
                for other in (None, SENTINEL, 0):
                    classic = [other, other]
                    classic[index] = value
                    # Both invalid fields produce the first field's diagnostic.
                    first = 0 if index == 1 and other == 0 else index
                    declared = classic[first]
                    resolved = (self.size, self.offset)[first]
                    name = ("size", "offset")[first]
                    with self.subTest(field=field, value=value, other=other):
                        self.reject(dict(classic=classic),
                                    f"classic EOCD central-directory {name} {declared}, "
                                    f"ZIP64 EOCD central-directory {name} {resolved}")
        # The 64-bit declaration is literal, including 0xffffffff, never a sentinel.
        for index, field in enumerate(("size", "offset")):
            for value in (SENTINEL, 2**32, 2**64 - 1):
                resolved = [self.size, self.offset]
                resolved[index] = value
                actual = (self.size, self.offset)[index]
                with self.subTest(field=field, resolved=value):
                    self.reject(dict(classic=(None, None), resolved=resolved),
                                f"classic EOCD central-directory {field} {actual}, "
                                f"ZIP64 EOCD central-directory {field} {value}", readable=False)

    def test_unresolved_classic_sentinels(self):
        for classic, field in (((SENTINEL, None), "size"), ((None, SENTINEL), "offset"),
                               ((SENTINEL, SENTINEL), "size")):
            with self.subTest(classic=classic):
                self.reject(dict(zip64=False, classic=classic),
                            f"classic EOCD central-directory {field} sentinel 4294967295 "
                            "requires a readable ZIP64 EOCD", readable=False)

    def test_raw_offset_agreement_with_prefixes_and_comments(self):
        for prefix in (b"", b"prepended data"):
            for comment in (b"", b"comment", b"x" * 65535):
                for prefix_offsets in (False, True):
                    for index, field in enumerate(("size", "offset")):
                        actual = (self.size, self.offset + (len(prefix) if prefix_offsets else 0))[index]
                        classic = [None, None]
                        classic[index] = actual + 1
                        with self.subTest(prefix=prefix, comment=len(comment), physical=prefix_offsets, field=field):
                            self.reject(dict(classic=classic, prefix=prefix, comment=comment,
                                             prefix_offsets=prefix_offsets),
                                        f"classic EOCD central-directory {field} {actual + 1}, "
                                        f"ZIP64 EOCD central-directory {field} {actual}")
        # A classic offset adjusted by exactly the prefix must NOT match raw ZIP64.
        self.reject(dict(classic=(None, self.offset + 14), prefix=b"prepended data"),
                    f"classic EOCD central-directory offset {self.offset + 14}, "
                    f"ZIP64 EOCD central-directory offset {self.offset}")

    def test_invalid_completed_candidates_removed(self):
        self.source.write_bytes(self.base)
        for classic in ((0, SENTINEL), (SENTINEL, 0), (0, 0),
                        ("higher", SENTINEL), (SENTINEL, "higher"), ("higher", "higher")):
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
                    raw = candidate.read_bytes()
                    size, offset = struct.unpack_from("<II", raw, len(raw) - 10)
                    values = tuple(actual + 1 if value == "higher" else value
                                   for value, actual in zip(classic, (size, offset)))
                    candidate.write_bytes(directory_end(raw, classic=values))
                    first = 0 if values[0] != SENTINEL else 1
                    self.candidate_reason = mismatch(candidate, ("size", "offset")[first],
                                                     values[first], (size, offset)[first])
                    with patch("qawrench.trace_budget.archive._verified_chunks",
                               side_effect=AssertionError("decoded invalid candidate")):
                        return inspect_archive(candidate, **kwargs)

                with self.subTest(classic=classic, force=force), patch(
                        "qawrench.trace_budget.archive.inspect_archive", side_effect=corrupt_candidate):
                    code, stdout, stderr = self.cli(["repack", self.source, "--out", target,
                                                     *(["--force"] if force else [])])
                    self.assertEqual((code, stdout, stderr), (1, "", "qawrench trace-budget: " + self.candidate_reason + "\n"))
                    self.assertEqual(len(candidates), 1)
                    self.assertFalse(candidates[0].exists())
                    self.preserved(self.base)
                    if not force:
                        self.assertFalse(target.exists())

    def test_matching_literals_and_independent_sentinels_preserve_reports_and_bytes(self):
        for method in (0, 8):
            for member_zip64 in (False, True):
                for descriptor, signed in ((False, False), (True, False), (True, True)):
                    base, data = synthetic_zip(method=method, zip64=member_zip64,
                        central_zip64=member_zip64, descriptor=descriptor, signed=signed,
                        member_flags=0x800, body_name="resources/雪😀")
                    for prefix_offsets in (False, True):
                        for end_zip64, classic in ((False, (None, None)), (True, (None, None)),
                                (True, (None, SENTINEL)), (True, (SENTINEL, None)), (True, (SENTINEL, SENTINEL))):
                            with self.subTest(method=method, member_zip64=member_zip64,
                                             descriptor=descriptor, signed=signed, classic=classic,
                                             end_zip64=end_zip64, physical=prefix_offsets):
                                self.source.write_bytes(base)
                                expected = inspect_archive(self.source)
                                raw = directory_end(base, zip64=end_zip64, classic=classic,
                                    prefix=b"prepended data", comment=b"comment", prefix_offsets=prefix_offsets)
                                self.source.write_bytes(raw)
                                overhead = len(raw) - len(base)
                                for key in ("archive_bytes", "zip_overhead_bytes"):
                                    expected[key] += overhead
                                expected["budget"]["measured_bytes"] += overhead
                                self.assertEqual(inspect_archive(self.source), expected)
                                result = repack_archive(self.source, self.output, force=True)
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
