"""Extraction requirements: independent local/central encodings, not zipfile repair."""

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


class ExtractionVersionTests(unittest.TestCase):
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

    def reject(self, options, reason, name="resources/body"):
        raw, _ = synthetic_zip(**options)
        self.source.write_bytes(raw)
        self.output.write_bytes(b"existing destination")
        good_bytes = self.good.read_bytes()
        for operation in (lambda: inspect_archive(self.source),
                          lambda: compare_archives(self.source, self.good),
                          lambda: compare_archives(self.good, self.source),
                          lambda: repack_archive(self.source, self.output, force=True)):
            with self.assertRaisesRegex(ArchiveError, reason + ".*" + name):
                operation()
            self.assertEqual(self.source.read_bytes(), raw)
            self.assertEqual(self.good.read_bytes(), good_bytes)
            self.assertEqual(self.output.read_bytes(), b"existing destination")
            self.assertEqual(list(self.root.glob(".*.tmp")), [])
        for args in (["inspect", self.source], ["compare", self.source, self.good],
                     ["compare", self.good, self.source], ["repack", self.source],
                     ["repack", self.source, "--dry-run"]):
            with self.subTest(command=args[0], dry_run="--dry-run" in args):
                code, stdout, stderr = self.cli([*args, "--out", self.output, "--force"])
                self.assertEqual(code, 1)
                self.assertEqual(stdout, "")
                self.assertIn("qawrench trace-budget: ", stderr)
                self.assertRegex(stderr, reason + ".*" + name)
                self.assertEqual(self.source.read_bytes(), raw)
                self.assertEqual(self.good.read_bytes(), good_bytes)
                self.assertEqual(self.output.read_bytes(), b"existing destination")
                self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_confirmed_local_99_central_20_is_rejected(self):
        self.reject({"local_version": 99, "central_version": 20},
                    "local ZIP extraction version mismatch")

    def test_extraction_versions_must_agree_in_both_bytes(self):
        for local, central in ((10, 20), (20, 10), (45, 20), (20, 45),
                               (0x0114, 20), (20, 0x0114)):
            with self.subTest(local=local, central=central):
                self.reject({"local_version": local, "central_version": central},
                            "local ZIP extraction version mismatch")

    def test_feature_minima_for_stored_deflated_directories_and_zip64(self):
        cases = [(dict(method=0), (0, 9)), (dict(method=8), (10, 19)),
                 (dict(method=0, body_name="resources/", body=b""), (10, 19))]
        for method in (0, 8):
            for descriptor in (False, True):
                for layout in ({"zip64": True}, {"central_zip64": True}):
                    cases.append((dict(method=method, descriptor=descriptor, **layout), (20, 44)))
        for options, versions in cases:
            for version in versions:
                with self.subTest(options=options, version=version):
                    self.reject(dict(options, local_version=version, central_version=version),
                                "ZIP extraction version below feature minimum",
                                options.get("body_name", "resources/body"))

    def test_higher_unsupported_requirements_are_named(self):
        # 63 is readable by zipfile, but our supported member features end at 45.
        for version in (46, 50, 63, 0x0114):
            with self.subTest(version=version):
                self.reject({"local_version": version, "central_version": version},
                            "unsupported ZIP extraction version")

    def test_invalid_candidate_requirement_cleans_actual_temporary_archive(self):
        raw, _ = synthetic_zip()
        self.source.write_bytes(raw)
        self.output.write_bytes(b"existing destination")
        candidates = []

        def corrupt_candidate(path, **kwargs):
            candidate = Path(path)
            self.assertTrue(candidate.is_file())
            self.assertTrue(candidate.name.endswith(".tmp"))
            candidates.append(candidate)
            with zipfile.ZipFile(candidate) as archive:
                offset = archive.getinfo("resources/body").header_offset
            content = bytearray(candidate.read_bytes())
            struct.pack_into("<H", content, offset + 4, 99)
            candidate.write_bytes(content)
            return inspect_archive(candidate, **kwargs)

        # Corrupt a real completed candidate before its normal validation scan.
        with patch("qawrench.trace_budget.archive.inspect_archive", side_effect=corrupt_candidate):
            with self.assertRaisesRegex(ArchiveError, "local ZIP extraction version mismatch.*resources/body"):
                repack_archive(self.source, self.output, force=True)
        self.assertEqual(len(candidates), 1)
        self.assertFalse(candidates[0].exists())
        self.assertEqual(self.source.read_bytes(), raw)
        self.assertEqual(self.output.read_bytes(), b"existing destination")
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_matching_supported_versions_preserve_reports_and_every_payload(self):
        layouts = [(dict(method=0), (10, 11, 19, 20, 44, 45)),
                   (dict(method=8), (20, 21, 44, 45)),
                   (dict(method=0, body_name="resources/", body=b""), (20, 45))]
        for method in (0, 8):
            layouts.extend([(dict(method=method, zip64=True), (45,)),
                            (dict(method=method, central_zip64=True), (45,)),
                            (dict(method=method, zip64=True, central_zip64=True), (45,))])
        for options, versions in layouts:
            for version in versions:
                for descriptor, signed in ((False, False), (True, False), (True, True)):
                    with self.subTest(options=options, version=version,
                                      descriptor=descriptor, signed=signed):
                        raw, data = synthetic_zip(**options, descriptor=descriptor, signed=signed,
                                                  local_version=version, central_version=version)
                        self.source.write_bytes(raw)
                        report = inspect_archive(self.source)
                        self.assertTrue(report["verified"]["crc32"])
                        self.assertEqual({m["name"]: m["sha256"] for m in report["members"]},
                                         {n: hashlib.sha256(p).hexdigest() for n, p in data.items()})
                        result = repack_archive(self.source, self.output, force=True)
                        self.assertTrue(result["all_member_hashes_equal"])
                        with zipfile.ZipFile(self.output) as archive:
                            self.assertEqual({n: archive.read(n) for n in archive.namelist()}, data)
                        comparison = compare_archives(self.source, self.output)
                        self.assertEqual(comparison, result["comparison"])
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
