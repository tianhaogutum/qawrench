"""Complete central extra-field framing, independently of local ZIP64 layout."""

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
from .test_zip_end_comments import comment_end
from qawrench.trace_budget import ArchiveError, compare_archives, inspect_archive, repack_archive
from qawrench.trace_budget.archive import _verified_chunks
from qawrench.trace_budget.cli import main


UNKNOWN = bytes.fromhex("feca0000efbe0300726177")


def central_extra(raw, extra, *, member="resources/body", fields=()):
    """Replace one central extra area; retain all local records and payloads.

    Accept an ordinary classic-end fixture, then callers may add ZIP64 ends,
    prefixes/comments. Optional (fixed-header offset, value) pairs independently
    select conditional central ZIP64 sentinels without changing local headers.
    """
    end = bytearray(raw[-22:])
    size, start = struct.unpack_from("<II", end, 12)
    assert start + size == len(raw) - 22
    directory = bytearray()
    position = start
    found = False
    while position < start + size:
        header = bytearray(raw[position:position + 46])
        assert header[:4] == b"PK\x01\x02"
        names, extras, comments = struct.unpack_from("<HHH", header, 28)
        name = raw[position + 46:position + 46 + names]
        old = raw[position + 46 + names:position + 46 + names + extras]
        comment = raw[position + 46 + names + extras:position + 46 + names + extras + comments]
        target = name.decode("utf-8" if struct.unpack_from("<H", header, 8)[0] & 0x800 else "cp437") == member
        if target:
            found = True
            struct.pack_into("<H", header, 30, len(extra))
            for offset, value in fields:
                struct.pack_into("<I", header, offset, value)
        directory.extend(header + name + (extra if target else old) + comment)
        position += 46 + names + extras + comments
    assert found
    struct.pack_into("<I", end, 12, len(directory))
    return raw[:start] + directory + end


def truncated_extra(path, member="resources/body"):
    return f"invalid ZIP central extra: {str(path)!r}; {member!r}; truncated field header"


def supported_cases():
    """Unknown fields and every supported conditional central ZIP64 layout."""
    for method in (0, 8):
        for local64 in (False, True):
            for descriptor, signed in ((False, False), (True, False), (True, True)):
                name = "resources/雪😀"
                raw, data = synthetic_zip(method=method, zip64=local64, central_zip64=local64,
                    descriptor=descriptor, signed=signed, member_flags=0x800, body_name=name)
                with zipfile.ZipFile(io.BytesIO(raw)) as z:
                    extra = z.getinfo(name).extra
                for tag, area in (("empty", extra), ("zero", extra + bytes.fromhex("feca0000")),
                                  ("unknown", UNKNOWN + extra + UNKNOWN)):
                    yield f"m{method}-local64{local64}-dd{descriptor}-signed{signed}-{tag}", central_extra(raw, area, member=name), data
        # Central ZIP64 has conditional, not fixed two-size local framing.
        raw, data = synthetic_zip(method=method, local_version=45, central_version=45)
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            info = z.getinfo("resources/body")
        for mask in range(8):
            fields, values = [], []
            for bit, offset, value in ((1, 24, info.file_size), (2, 20, info.compress_size), (4, 42, info.header_offset)):
                if mask & bit:
                    fields.append((offset, 0xFFFFFFFF))
                    values.append(value)
            area = UNKNOWN + struct.pack("<HH", 1, 8 * len(values)) + struct.pack("<" + "Q" * len(values), *values)
            yield f"m{method}-conditional{mask}", central_extra(raw, area, fields=fields), data


class CentralExtraTests(unittest.TestCase):
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

    def confirmed(self, length):
        raw = central_extra(self.base, bytes.fromhex("feca0000")[:length])
        self.source.write_bytes(raw)
        # All payloads are valid independently; only the central TLV is partial.
        with zipfile.ZipFile(self.source) as z:
            self.assertEqual({n: z.read(n) for n in z.namelist()}, self.data)
        with self.assertRaisesRegex(ArchiveError, re.escape(truncated_extra(self.source))):
            inspect_archive(self.source)

    def test_confirmed_one_byte_header(self):
        self.confirmed(1)

    def test_confirmed_two_byte_header(self):
        self.confirmed(2)

    def test_confirmed_three_byte_header(self):
        self.confirmed(3)

    def test_standalone_and_trailing_headers_reject_all_operations_before_any_decoding(self):
        for local64 in (False, True):
            base, _ = synthetic_zip(zip64=local64, central_zip64=local64)
            with zipfile.ZipFile(io.BytesIO(base)) as z:
                valid64 = z.getinfo("resources/body").extra
            for complete in (valid64, UNKNOWN + valid64):
                for length in (1, 2, 3):
                    raw = central_extra(base, complete + bytes.fromhex("feca0000")[:length])
                    raw = comment_end(raw, zip64=local64, prefix=b"prepended data", comment=b"comment")
                    self.source.write_bytes(raw)
                    reason = truncated_extra(self.source)

                    def guard(archive, handle, *args):
                        self.assertNotEqual(Path(handle.name), self.source, "decoded invalid archive")
                        return _verified_chunks(archive, handle, *args)

                    with self.subTest(local64=local64, complete=complete.hex(), length=length), patch(
                            "qawrench.trace_budget.archive._verified_chunks", side_effect=guard):
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
                                self.assertEqual(self.cli([*args, "--out", target, *(["--force"] if force else [])]),
                                                 (1, "", "qawrench trace-budget: " + reason + "\n"))
                                if not force:
                                    self.assertFalse(target.exists())
                                self.preserved(raw)

    def test_invalid_completed_candidates_are_physically_removed(self):
        self.source.write_bytes(self.base)
        for complete in (b"", UNKNOWN):
            for length in (1, 2, 3):
                for force in (False, True):
                    candidates, reasons = [], []
                    target = self.output if force else self.root / "absent.zip"

                    def corrupt_candidate(path, **kwargs):
                        candidate = Path(path)
                        self.assertTrue(candidate.is_file())
                        self.assertTrue(candidate.name.endswith(".tmp"))
                        with zipfile.ZipFile(candidate) as z:
                            self.assertEqual({n: z.read(n) for n in z.namelist()}, self.data)
                        candidates.append(candidate)
                        candidate.write_bytes(central_extra(candidate.read_bytes(), complete + bytes.fromhex("feca0000")[:length]))
                        reasons.append(truncated_extra(candidate))
                        with patch("qawrench.trace_budget.archive._verified_chunks", side_effect=AssertionError("decoded invalid candidate")):
                            return inspect_archive(candidate, **kwargs)

                    with self.subTest(complete=complete.hex(), length=length, force=force), patch(
                            "qawrench.trace_budget.archive.inspect_archive", side_effect=corrupt_candidate):
                        result = self.cli(["repack", self.source, "--out", target, *(["--force"] if force else [])])
                        self.assertEqual(result, (1, "", "qawrench trace-budget: " + reasons[0] + "\n"))
                        self.assertEqual(len(candidates), 1)
                        self.assertFalse(candidates[0].exists())
                        self.preserved(self.base)
                        if not force:
                            self.assertFalse(target.exists())

    def test_complete_unknown_and_conditional_zip64_fields_preserve_reports_and_payloads(self):
        for label, raw, data in supported_cases():
            with self.subTest(case=label):
                self.source.write_bytes(raw)
                expected = inspect_archive(self.source)
                decorated = comment_end(raw, zip64=True, prefix=b"prepended data", prefix_offsets="conditional" not in label, comment=b"comment")
                for key in ("archive_bytes", "zip_overhead_bytes"):
                    expected[key] += len(decorated) - len(raw)
                expected["budget"]["measured_bytes"] += len(decorated) - len(raw)
                self.source.write_bytes(decorated)
                self.assertEqual(inspect_archive(self.source), expected)
                result = repack_archive(self.source, self.output, force=True)
                for path in (self.source, self.output):
                    with zipfile.ZipFile(path) as z:
                        self.assertEqual({n: z.read(n) for n in z.namelist()}, data)
                self.assertEqual(compare_archives(self.source, self.output), result["comparison"])
                self.assertTrue(compare_archives(self.output, self.source)["all_retained_hashes_equal"])
                dry = self.root / "dry.zip"
                code, stdout, stderr = self.cli(["repack", self.source, "--out", dry, "--dry-run"])
                self.assertEqual((code, stderr), (0, ""))
                self.assertTrue(json.loads(stdout)["dry_run"])
                self.assertFalse(dry.exists())
                self.assertEqual(self.source.read_bytes(), decorated)
                self.assertEqual(list(self.root.glob(".*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
