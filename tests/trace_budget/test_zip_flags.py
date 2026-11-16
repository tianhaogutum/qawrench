"""Conservative, method-specific ZIP flag contracts on independent ZIP records."""

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from .test_zip_headers import synthetic_zip
from qawrench.trace_budget import ArchiveError, compare_archives, inspect_archive, repack_archive
from qawrench.trace_budget.cli import main


def replace_member_flags(raw, flags, name="resources/body"):
    """Patch both records, including an actual repack candidate, without rewriting."""
    content = bytearray(raw)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        local = archive.getinfo(name).header_offset
        position = archive.start_dir
        for _ in archive.infolist():
            assert content[position:position + 4] == b"PK\x01\x02"
            name_length, extra_length, comment_length = struct.unpack_from("<HHH", content, position + 28)
            offset = struct.unpack_from("<I", content, position + 42)[0]
            if offset == local:
                struct.pack_into("<H", content, position + 8, flags)
                break
            position += 46 + name_length + extra_length + comment_length
        else:
            raise AssertionError("central record not found")
    struct.pack_into("<H", content, local + 6, flags)
    return bytes(content)


class GeneralPurposeFlagTests(unittest.TestCase):
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

    def reject(self, flags, method=8, descriptor=False):
        raw, _ = synthetic_zip(method=method, descriptor=descriptor, member_flags=flags)
        self.source.write_bytes(raw)
        self.output.write_bytes(b"existing destination")
        good_bytes = self.good.read_bytes()
        unsupported = flags & ~(0x080E if method == 8 else 0x0808)
        reason = rf"unsupported ZIP flags: 'resources/body'; unsupported mask 0x{unsupported:04x}"
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

    def assert_preserved(self, raw, good_bytes):
        self.assertEqual(self.source.read_bytes(), raw)
        self.assertEqual(self.good.read_bytes(), good_bytes)
        self.assertEqual(self.output.read_bytes(), b"existing destination")
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_confirmed_matching_masked_header_bit_13(self):
        self.reject(0x2000)

    def test_every_unsupported_bit_and_combined_masks(self):
        for method, allowed in ((0, 0x0808), (8, 0x080E)):
            for bit in range(16):
                if allowed & (1 << bit):
                    continue
                with self.subTest(method=method, bit=bit):
                    self.reject(1 << bit, method)
            # Diagnostic must name the unsupported subset, excluding valid bits.
            with self.subTest(method=method, combined=True):
                self.reject(0xA841 | (6 if method == 8 else 0), method, descriptor=True)

    def test_invalid_candidate_flags_clean_actual_temporary_archive(self):
        raw, _ = synthetic_zip()
        self.source.write_bytes(raw)
        self.output.write_bytes(b"existing destination")
        candidates = []

        def corrupt_candidate(path, **kwargs):
            candidate = Path(path)
            self.assertTrue(candidate.is_file())
            self.assertTrue(candidate.name.endswith(".tmp"))
            candidates.append(candidate)
            candidate.write_bytes(replace_member_flags(candidate.read_bytes(), 0x2000))
            return inspect_archive(candidate, **kwargs)

        # CLI must also keep stdout empty and preserve the forced destination
        # when a completed candidate, not the source, fails the normal scan.
        with patch("qawrench.trace_budget.archive.inspect_archive", side_effect=corrupt_candidate):
            code, stdout, stderr = self.cli(["repack", self.source, "--out", self.output, "--force"])
        self.assertEqual((code, stdout), (1, ""))
        self.assertRegex(stderr, "unsupported ZIP flags: 'resources/body'; unsupported mask 0x2000")
        self.assertEqual(len(candidates), 1)
        self.assertFalse(candidates[0].exists())
        self.assert_preserved(raw, self.good.read_bytes())

    def test_supported_flags_preserve_reports_and_exact_payloads(self):
        for method, options in ((0, (0,)), (8, (0, 2, 4, 6))):
            for option in options:
                for encoding, name in ((0, "resources/café"), (0x800, "resources/雪😀")):
                    for zip64 in (False, True):
                        for descriptor, signed in ((False, False), (True, False), (True, True)):
                            with self.subTest(method=method, option=option, encoding=encoding,
                                              zip64=zip64, descriptor=descriptor, signed=signed):
                                kwargs = dict(method=method, zip64=zip64, central_zip64=zip64,
                                              descriptor=descriptor, signed=signed, body_name=name)
                                raw, data = synthetic_zip(**kwargs, member_flags=encoding | option)
                                self.source.write_bytes(raw)
                                report = inspect_archive(self.source)
                                self.assertEqual({m["name"]: m["sha256"] for m in report["members"]},
                                                 {n: hashlib.sha256(p).hexdigest() for n, p in data.items()})
                                # Flags do not add or alter inventory/report fields.
                                self.good.write_bytes(synthetic_zip(**kwargs, member_flags=encoding)[0])
                                self.assertEqual(report, inspect_archive(self.good))
                                result = repack_archive(self.source, self.output, force=True)
                                self.assertTrue(result["all_member_hashes_equal"])
                                with zipfile.ZipFile(self.output) as archive:
                                    self.assertEqual({n: archive.read(n) for n in archive.namelist()}, data)
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
