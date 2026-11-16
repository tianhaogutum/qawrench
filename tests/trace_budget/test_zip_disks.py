"""Single-file member placement requirements, independently encoded on disk."""

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


def replace_member_disk(raw, disk, name="resources/body", disk_extra=None):
    """Patch a central disk field, optionally appending a ZIP64 disk value.

    Walk raw records, without relying on ZipInfo.volume or the production reader.
    The helper also handles completed repack candidates. disk_extra is the raw
    32-bit disk suffix (or deliberately truncated bytes) of a ZIP64 extra block.
    """
    content = bytearray(raw)
    end = len(content) - 22  # All these synthetic archives have no ZIP comment.
    assert content[end:end + 4] == b"PK\x05\x06"
    position = struct.unpack_from("<I", content, end + 16)[0]
    count = struct.unpack_from("<H", content, end + 10)[0]
    for _ in range(count):
        assert content[position:position + 4] == b"PK\x01\x02"
        n, e, c = struct.unpack_from("<HHH", content, position + 28)
        flags = struct.unpack_from("<H", content, position + 8)[0]
        decoded = content[position + 46:position + 46 + n].decode("utf-8" if flags & 0x800 else "cp437")
        if decoded == name:
            struct.pack_into("<H", content, position + 34, disk)
            if disk_extra is not None:
                start = position + 46 + n
                extra = content[start:start + e]
                if extra:
                    # synthetic_zip's optional central extra contains just sizes.
                    assert struct.unpack_from("<HH", extra) == (1, 16)
                    payload = extra[4:] + disk_extra
                else:
                    payload = disk_extra
                new = struct.pack("<HH", 1, len(payload)) + payload
                content[start:start + e] = new
                struct.pack_into("<H", content, position + 30, len(new))
                delta = len(new) - e
                end += delta
                directory_size = struct.unpack_from("<I", content, end + 12)[0]
                struct.pack_into("<I", content, end + 12, directory_size + delta)
            return bytes(content)
        position += 46 + n + e + c
    raise AssertionError("central member not found")


class DiskStartTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / "source.zip"
        self.good = self.root / "good.zip"
        self.output = self.root / "output.zip"
        self.good.write_bytes(synthetic_zip()[0])

    def cli(self, args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(list(map(str, args)))
        return code, stdout.getvalue(), stderr.getvalue()

    def assert_preserved(self, raw, good_bytes):
        self.assertEqual(self.source.read_bytes(), raw)
        self.assertEqual(self.good.read_bytes(), good_bytes)
        self.assertEqual(self.output.read_bytes(), b"existing destination")
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def reject(self, disk, *, name="resources/body", disk_extra=None, **options):
        raw = replace_member_disk(synthetic_zip(**options)[0], disk, name, disk_extra)
        self.source.write_bytes(raw)
        self.output.write_bytes(b"existing destination")
        good_bytes = self.good.read_bytes()
        reason = re.escape(f"unsupported ZIP disk start: {name!r}; declared {disk}, supported 0")
        for operation in (lambda: inspect_archive(self.source),
                          lambda: compare_archives(self.source, self.good),
                          lambda: compare_archives(self.good, self.source),
                          lambda: repack_archive(self.source, self.output, force=True)):
            with self.assertRaisesRegex(ArchiveError, reason):
                operation()
            self.assert_preserved(raw, good_bytes)
        for args in (["inspect", self.source], ["compare", self.source, self.good],
                     ["compare", self.good, self.source], ["repack", self.source],
                     ["repack", self.source, "--dry-run"]):
            with self.subTest(command=args[0], dry_run="--dry-run" in args):
                code, stdout, stderr = self.cli([*args, "--out", self.output, "--force"])
                self.assertEqual((code, stdout), (1, ""))
                self.assertRegex(stderr, "qawrench trace-budget: " + reason)
                self.assert_preserved(raw, good_bytes)

    def test_confirmed_member_on_disk_1(self):
        self.reject(1)

    def test_nonzero_disks_and_unresolved_zip64_sentinel(self):
        for disk in (2, 0x7FFF, 0xFFFE, 0xFFFF):
            with self.subTest(disk=disk):
                self.reject(disk)
        # The ZIP64 disk-start sentinel is unsupported even if an extra resolves
        # to zero. zipfile leaves volume at 65535 and does not decode this field.
        for sizes in (False, True):
            for suffix in (b"", b"\x00\x00", struct.pack("<I", 0),
                           struct.pack("<I", 1), struct.pack("<I", 65536)):
                with self.subTest(zip64_sizes=sizes, suffix=suffix):
                    self.reject(0xFFFF, disk_extra=suffix, central_zip64=sizes,
                                local_version=45, central_version=45)
        for name in ("0.trace", "tail"):
            with self.subTest(name=name):
                self.reject(1, name=name)

    def test_invalid_candidate_disk_cleans_actual_temporary_archive(self):
        raw, _ = synthetic_zip()
        self.source.write_bytes(raw)
        self.output.write_bytes(b"existing destination")
        for disk in (1, 0xFFFF):
            candidates = []

            def corrupt_candidate(path, **kwargs):
                candidate = Path(path)
                self.assertTrue(candidate.is_file())
                self.assertTrue(candidate.name.endswith(".tmp"))
                candidates.append(candidate)
                candidate.write_bytes(replace_member_disk(candidate.read_bytes(), disk))
                return inspect_archive(candidate, **kwargs)

            with self.subTest(disk=disk), patch("qawrench.trace_budget.archive.inspect_archive",
                                              side_effect=corrupt_candidate):
                code, stdout, stderr = self.cli(["repack", self.source, "--out", self.output, "--force"])
                self.assertEqual((code, stdout), (1, ""))
                self.assertIn(f"unsupported ZIP disk start: 'resources/body'; declared {disk}, supported 0", stderr)
                self.assertEqual(len(candidates), 1)
                self.assertFalse(candidates[0].exists())
                self.assert_preserved(raw, self.good.read_bytes())

    def test_disk_zero_controls_preserve_reports_and_exact_payloads(self):
        for method, options in ((0, (0,)), (8, (0, 2, 4, 6))):
            for option in options:
                for encoding, name in ((0, "resources/café"), (0x800, "resources/雪😀")):
                    for zip64 in (False, True):
                        for descriptor, signed in ((False, False), (True, False), (True, True)):
                            with self.subTest(method=method, option=option, encoding=encoding,
                                              zip64=zip64, descriptor=descriptor, signed=signed):
                                raw, data = synthetic_zip(method=method, member_flags=encoding | option,
                                    body_name=name, zip64=zip64, central_zip64=zip64,
                                    descriptor=descriptor, signed=signed)
                                self.source.write_bytes(raw)
                                self.good.write_bytes(replace_member_disk(raw, 0, name))
                                report = inspect_archive(self.source)
                                self.assertEqual(report, inspect_archive(self.good))
                                self.assertEqual({m["name"]:m["sha256"] for m in report["members"]},
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


if __name__ == "__main__":
    unittest.main()
