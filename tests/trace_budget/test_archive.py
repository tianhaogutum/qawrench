"""Substantive offline tests; every archive is synthetic, never a real recording."""

from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile
import zlib

from examples.trace_budget.generate_trace import generate, members, ndjson
from qawrench.trace_budget import ArchiveError, Limits, inspect_archive, repack_archive
from qawrench.trace_budget.archive import CHUNK_BYTES, _publish, write_report


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.zip"
        self.output = self.root / "output.zip"
        self.data = members()

    def zip(self, entries=None, *, compression=zipfile.ZIP_STORED):
        if entries is None:
            entries = self.data.items()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(self.source, "w", compression=compression) as archive:
                for name, payload in entries:
                    archive.writestr(name, payload)
        return self.source

    def cli(self, *args, cwd=None):
        return subprocess.run([sys.executable, "-m", "qawrench.trace_budget", *map(str, args)],
                              text=True, capture_output=True, cwd=cwd, timeout=30)

    def assert_rejected(self, pattern):
        with self.assertRaisesRegex(ArchiveError, pattern):
            inspect_archive(self.source)
        with self.assertRaisesRegex(ArchiveError, pattern):
            repack_archive(self.source, self.output)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_inventory_classification_top_budget_and_hashes(self):
        # A resource named *.trace is still a resource, never parsed as events.
        self.data["resources/image.trace"] = b"arbitrary binary\xff"
        self.data["notes/readme.txt"] = b"synthetic note"
        self.zip()
        report = inspect_archive(self.source, top=2, budget_bytes=1)
        self.assertTrue(report["budget"]["exceeded"])
        self.assertEqual(report["archive_bytes"], self.source.stat().st_size)
        self.assertEqual(report["stored_bytes"], sum(map(len, self.data.values())))
        self.assertEqual(report["uncompressed_bytes"], report["stored_bytes"])
        self.assertEqual(report["zip_overhead_bytes"], report["archive_bytes"] - report["stored_bytes"])
        self.assertEqual(report["allocation"]["resources"]["member_count"], 2)
        self.assertEqual(report["allocation"]["network"]["member_count"], 1)
        self.assertEqual(len(report["top_members"]), 2)
        expected = sorted(self.data, key=lambda name: (-len(self.data[name]), name))[:2]
        self.assertEqual([m["name"] for m in report["top_members"]], expected)
        for member in report["members"]:
            self.assertEqual(member["sha256"], hashlib.sha256(self.data[member["name"]]).hexdigest())
        trace = next(m for m in report["members"] if m["category"] == "trace")
        self.assertEqual(trace["ndjson"]["event_count"], 2401)
        self.assertEqual(trace["ndjson"]["trace_version"], 8)
        self.assertFalse(inspect_archive(self.source, budget_bytes=report["archive_bytes"])["budget"]["exceeded"])
        for basis in ("stored", "uncompressed"):
            self.assertFalse(inspect_archive(self.source, budget_bytes=report["stored_bytes"], budget_basis=basis)["budget"]["exceeded"])

    def test_repack_e2e_crc_hashes_screenshot_unknown_fields_and_determinism(self):
        # Original synthetic 1px PNG fixture, embedded as a screencast resource.
        def png_chunk(tag, data):
            return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

        png = (b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
               + png_chunk(b"IDAT", zlib.compress(b"\x00\x24\x00\x00")) + png_chunk(b"IEND", b""))
        self.data["resources/synthetic-frame.png"] = png
        self.data["trace.trace"] += ndjson([
            {"type": "screencast-frame", "pageId": "page@synthetic", "sha1": "synthetic-frame.png",
             "width": 1, "height": 1, "timestamp": 101, "extensionField": {"arbitrary": "保留"}},
            {"type": "extension-event", "custom": [1, 2, {"keep": "byte for byte"}]},
        ])
        self.data["resources/"] = b""
        self.data["notes/über.txt"] = b"synthetic utf8 filename"
        self.zip(reversed(list(self.data.items())))
        report = repack_archive(self.source, self.output)
        self.assertTrue(report["all_member_hashes_equal"])
        self.assertEqual(report["verified_member_count"], len(self.data))
        self.assertLess(report["new_bytes"], report["original_bytes"])
        with zipfile.ZipFile(self.output) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(archive.namelist(), sorted(self.data))
            for info in archive.infolist():
                self.assertEqual(archive.read(info), self.data[info.filename])
                self.assertEqual(info.date_time, (1980, 1, 1, 0, 0, 0))
                self.assertEqual(info.compress_type, zipfile.ZIP_DEFLATED)
                self.assertEqual(info.comment, b"")
        second = self.root / "second.zip"
        repack_archive(self.source, second)
        self.assertEqual(self.output.read_bytes(), second.read_bytes())
        # Idempotent canonical repack: no fictional savings on already canonical input.
        third = self.root / "third.zip"
        noop = repack_archive(self.output, third)
        self.assertEqual(noop["outcome"], "unchanged")
        self.assertEqual(noop["saved_bytes"], 0)
        self.assertEqual(self.output.read_bytes(), third.read_bytes())

    def test_precompressed_binary_no_gain_and_incompressible_growth(self):
        for kind in ("precompressed-binary", "incompressible-binary"):
            with self.subTest(kind=kind):
                source = generate(self.root / f"{kind}.zip", kind)
                output = self.root / f"{kind}-out.zip"
                result = repack_archive(source, output)
                self.assertTrue(result["all_member_hashes_equal"])
                self.assertLessEqual(result["saved_bytes"], 0)
                self.assertGreaterEqual(result["ratio"], 1)
                self.assertEqual(result["outcome"], "unchanged" if kind == "precompressed-binary" else "larger")
                with zipfile.ZipFile(source) as before, zipfile.ZipFile(output) as after:
                    for name in before.namelist():
                        self.assertEqual(before.read(name), after.read(name))
                    resource = next(i for i in after.infolist() if i.filename.startswith("resources/"))
                    self.assertGreater(resource.compress_size, resource.file_size)

    def test_not_zip_truncated_empty_and_not_trace(self):
        for payload in (b"not zip", b"PK\x03\x04garbage"):
            self.source.write_bytes(payload)
            self.assert_rejected("invalid ZIP")
        self.zip([])
        self.assert_rejected("not a trace-shaped")
        self.zip([("resources/fake.trace", b"not NDJSON")])
        self.assert_rejected("not a trace-shaped")
        self.zip()
        self.source.write_bytes(self.source.read_bytes()[:-25])
        self.assert_rejected("invalid ZIP")

    def test_unsafe_member_names(self):
        for name in ("../evil", "/absolute", "a/../../evil", "a/./b", "a//b", "C:/bad", "a\\b", "a\nb", "//server/file"):
            with self.subTest(name=name):
                self.zip(list(self.data.items()) + [(name, b"payload")])
                self.assert_rejected("unsafe member")

    def test_nul_member_name(self):
        self.zip(list(self.data.items()) + [("abcXdef", b"payload")])
        self.source.write_bytes(self.source.read_bytes().replace(b"abcXdef", b"abc\0def"))
        self.assert_rejected("unsafe member")

    def test_duplicate_and_parent_collisions(self):
        for extra in ([('trace.trace', self.data['trace.trace'])], [("a", b"x"), ("a/child", b"y")],
                      [("a/", b""), ("a", b"x")]):
            with self.subTest(extra=[name for name, _ in extra]):
                self.zip(list(self.data.items()) + extra)
                self.assert_rejected("duplicate|parent")

    def test_special_and_nonempty_directory_members(self):
        for mode in (stat.S_IFLNK, stat.S_IFIFO, stat.S_IFCHR):
            info = zipfile.ZipInfo("link")
            info.create_system = 3
            info.external_attr = (mode | 0o777) << 16
            self.zip(list(self.data.items()) + [(info, b"target")])
            self.assert_rejected("special/symlink")
        self.zip(list(self.data.items()) + [("dir/", b"not empty")])
        self.assert_rejected("nonempty directory")

    def test_encrypted_flags(self):
        self.zip()
        content = bytearray(self.source.read_bytes())
        # Set encryption bits in both first local and central headers.
        for signature, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
            index = content.index(signature)
            flags = struct.unpack_from("<H", content, index + offset)[0]
            struct.pack_into("<H", content, index + offset, flags | 1)
        self.source.write_bytes(content)
        self.assert_rejected("encrypted")

    def test_corrupt_crc_and_local_header(self):
        self.zip()
        with zipfile.ZipFile(self.source) as archive:
            info = archive.getinfo("fixture-label.txt")
            position = info.header_offset
        content = bytearray(self.source.read_bytes())
        name_len, extra_len = struct.unpack_from("<HH", content, position + 26)
        payload_start = position + 30 + name_len + extra_len
        content[payload_start] ^= 0x01
        self.source.write_bytes(content)
        self.assert_rejected("CRC")
        self.zip()
        content = bytearray(self.source.read_bytes())
        content[30] ^= 0x01
        self.source.write_bytes(content)
        self.assert_rejected("differ")

    def encoded_resource(self, payload, encoded, *, declared_size=None, crc=None):
        """Local synthetic ZIP: inject raw DEFLATE bytes with chosen metadata."""
        context = self.data["trace.trace"].splitlines()[0] + b"\n"
        self.zip([("0.trace", context), ("resources/body", encoded)])
        with zipfile.ZipFile(self.source) as archive:
            local = archive.getinfo("resources/body").header_offset
            central = archive.start_dir
        raw = bytearray(self.source.read_bytes())
        name_len, extra_len, comment_len = struct.unpack_from("<HHH", raw, central + 28)
        central += 46 + name_len + extra_len + comment_len
        size = len(payload) if declared_size is None else declared_size
        checksum = zlib.crc32(payload) if crc is None else crc
        for position, method_offset, crc_offset, size_offset in (
                (local, 8, 14, 22), (central, 10, 16, 24)):
            struct.pack_into("<H", raw, position + method_offset, zipfile.ZIP_DEFLATED)
            struct.pack_into("<I", raw, position + crc_offset, checksum)
            struct.pack_into("<I", raw, position + size_offset, size)
        self.source.write_bytes(raw)

    def assert_corruption_preserves_files(self, pattern, **kwargs):
        original = self.source.read_bytes()
        self.output.write_bytes(b"keep existing destination")
        with self.assertRaisesRegex(ArchiveError, pattern):
            inspect_archive(self.source, **kwargs)
        with self.assertRaisesRegex(ArchiveError, pattern):
            repack_archive(self.source, self.output, force=True, **kwargs)
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(self.output.read_bytes(), b"keep existing destination")
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_truncated_deflate_with_matching_decoded_crc_is_rejected(self):
        payload = b"A" * 4096
        encoded = zlib.compress(payload, wbits=-15)[:-1]
        decoder = zlib.decompressobj(-15)
        self.assertEqual(decoder.decompress(encoded), payload)
        self.assertFalse(decoder.eof)
        self.encoded_resource(payload, encoded)
        self.assert_corruption_preserves_files("truncated DEFLATE.*resources/body")
        error = self.cli("repack", self.source, "--out", self.output, "--force")
        self.assertEqual(error.returncode, 1, error.stdout)
        self.assertIn("truncated DEFLATE", error.stderr)
        self.assertNotIn("Traceback", error.stderr)
        self.assertEqual(self.output.read_bytes(), b"keep existing destination")

    def test_deflate_cannot_hide_bytes_beyond_declared_size(self):
        payload = b"A" * 4096
        for size in (0, 64):
            with self.subTest(declared_size=size):
                self.encoded_resource(payload, zlib.compress(payload, wbits=-15),
                                      declared_size=size, crc=zlib.crc32(payload[:size]))
                self.assert_corruption_preserves_files("member size mismatch.*resources/body")

    def test_deflate_trailing_data_and_short_decoded_size_are_rejected(self):
        payload = b"A" * 4096
        for suffix in (b"junk", zlib.compress(b"hidden second stream", wbits=-15)):
            with self.subTest(suffix=suffix):
                self.encoded_resource(payload, zlib.compress(payload, wbits=-15) + suffix)
                self.assert_corruption_preserves_files("trailing compressed data.*resources/body")
        self.encoded_resource(payload, zlib.compress(payload, wbits=-15), declared_size=len(payload) + 1)
        self.assert_corruption_preserves_files("member size mismatch.*resources/body")

    def test_forged_deflate_output_is_bounded_at_member_and_total_limits(self):
        payload = b"A" * (4 * 1024 * 1024)
        declared = 1024
        context_size = len(self.data["trace.trace"].splitlines()[0]) + 1
        real_decoder = zlib.decompressobj
        decoded_lengths = []

        class MeasuredDecoder:
            def __init__(self, *args):
                self.decoder = real_decoder(*args)

            def __getattr__(self, key):
                return getattr(self.decoder, key)

            def decompress(self, data, max_length=0):
                self_assert.assertGreater(max_length, 0)
                result = self.decoder.decompress(data, max_length)
                decoded_lengths.append(len(result))
                return result

        self_assert = self
        cases = [(Limits(max_member_bytes=declared), "streamed member-size"),
                 (Limits(max_total_bytes=context_size + declared), "streamed total uncompressed-size")]
        for limits, message in cases:
            with self.subTest(message=message):
                self.encoded_resource(payload, zlib.compress(payload, wbits=-15),
                                      declared_size=declared, crc=zlib.crc32(payload[:declared]))
                decoded_lengths.clear()
                with patch("qawrench.trace_budget.archive.zlib.decompressobj", MeasuredDecoder):
                    self.assert_corruption_preserves_files(message + ".*resources/body", limits=limits)
                # The forged 4 MiB payload is stopped after 1,025 output bytes.
                self.assertEqual(max(decoded_lengths), declared + 1)

    def test_valid_deflate_chunk_boundaries_and_exact_size_limits(self):
        context_size = len(self.data["trace.trace"].splitlines()[0]) + 1
        for size in (0, CHUNK_BYTES - 1, CHUNK_BYTES, CHUNK_BYTES + 1, 3 * CHUNK_BYTES):
            with self.subTest(size=size):
                payload = b"A" * size
                self.encoded_resource(payload, zlib.compress(payload, wbits=-15))
                limits = Limits(max_member_bytes=max(size, context_size),
                                max_total_bytes=size + context_size, max_ratio=2000)
                report = inspect_archive(self.source, limits=limits)
                self.assertEqual(report["uncompressed_bytes"], size + context_size)
                result = repack_archive(self.source, self.output, limits=limits, force=True)
                self.assertTrue(result["all_member_hashes_equal"])
                with zipfile.ZipFile(self.output) as archive:
                    self.assertEqual(archive.read("resources/body"), payload)

    def test_streamed_descriptor_and_zip64_local_headers(self):
        class NonSeekable(io.BytesIO):
            def seek(self, *args):
                raise io.UnsupportedOperation("not seekable")

        # zipfile writes real data descriptors and ZIP64 local extra fields on
        # a nonseekable output; central-directory sizes remain authoritative.
        for method in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            with self.subTest(method=method):
                buffer = NonSeekable()
                with zipfile.ZipFile(buffer, "w", compression=method) as archive:
                    for name, payload in self.data.items():
                        with archive.open(name, "w", force_zip64=True) as writer:
                            writer.write(payload)
                self.source.write_bytes(buffer.getvalue())
                with zipfile.ZipFile(self.source) as archive:
                    self.assertTrue(all(i.flag_bits & 8 for i in archive.infolist()))
                    self.assertTrue(all(i.extract_version == 45 for i in archive.infolist()))
                self.assertTrue(inspect_archive(self.source)["verified"]["crc32"])
                result = repack_archive(self.source, self.output, force=True)
                self.assertTrue(result["all_member_hashes_equal"])
                with zipfile.ZipFile(self.output) as archive:
                    self.assertEqual({i.filename: archive.read(i) for i in archive.infolist()}, self.data)

    def test_deflate_crc_diagnostic_names_member_and_checksums(self):
        payload = b"A" * 4096
        self.encoded_resource(payload, zlib.compress(payload, wbits=-15), crc=0)
        expected = f"CRC32 mismatch: 'resources/body'; expected 00000000, got {zlib.crc32(payload):08x}"
        self.assert_corruption_preserves_files(expected)

    def test_unsupported_compression(self):
        # Set a non-supported central method without relying on optional bzip2/lzma.
        self.zip()
        content = bytearray(self.source.read_bytes())
        index = content.index(b"PK\x01\x02")
        struct.pack_into("<H", content, index + 10, 99)
        self.source.write_bytes(content)
        self.assert_rejected("unsupported ZIP compression")

    def test_corrupted_utf8_filename_is_a_clean_archive_error(self):
        self.zip(list(self.data.items()) + [("utf8Xname", b"x")])
        content = bytearray(self.source.read_bytes())
        position = 0
        while True:
            position = content.find(b"PK\x01\x02", position)
            if position < 0:
                break
            if content[position + 46:position + 55] == b"utf8Xname":
                flags = struct.unpack_from("<H", content, position + 8)[0]
                struct.pack_into("<H", content, position + 8, flags | 0x800)
                content[position + 46] = 0xff
                break
            position += 4
        self.source.write_bytes(content)
        self.assert_rejected("invalid ZIP")
        error = self.cli("inspect", self.source)
        self.assertEqual(error.returncode, 1)
        self.assertNotIn("Traceback", error.stderr)

    def test_limits_archive_members_member_total_line_and_ratio(self):
        self.zip()
        cases = [("max_archive_bytes", 10, "archive-size"), ("max_members", 1, "member-count"),
                 ("max_member_bytes", 1, "member-size"), ("max_total_bytes", 1, "total uncompressed"),
                 ("max_line_bytes", 5, "line-size")]
        for key, value, message in cases:
            with self.subTest(key=key), self.assertRaisesRegex(ArchiveError, message):
                inspect_archive(self.source, limits=replace(Limits(), **{key: value}))
        self.zip(compression=zipfile.ZIP_DEFLATED)
        with self.assertRaisesRegex(ArchiveError, "compression-ratio"):
            inspect_archive(self.source, limits=replace(Limits(), max_ratio=2))
        for kw in ({"max_ratio": float('nan')}, {"max_total_bytes": 0}, {"max_members": 1.5},
                   {"max_ratio": True}, {"max_members": 10**400}):
            with self.assertRaises(ArchiveError):
                Limits(**kw)

    def test_invalid_ndjson_and_unknown_versions(self):
        context = self.data["trace.trace"].splitlines()[0]
        invalid = [b"", b"{}\n", b"[]\n", b"null\n", b"not json\n", b"\xff\n", b"\xef\xbb\xbf" + context,
                   context + b"\n\n", context + b"\n{broken}", context + b'\n{"type":"log","x":NaN}',
                   context + b'\n{"type":"log","type":"after"}', context.replace(b'"version":8', b'"version":999'),
                   context.replace(b'"version":8', b'"version":true'), context + b'\n{"type":3}',
                   context + b'\n' + b'[' * 2000 + b']' * 2000]
        for data in invalid:
            with self.subTest(data=data[:50]):
                self.zip([(name, data if name == "trace.trace" else value) for name, value in self.data.items()])
                self.assert_rejected("NDJSON|empty .trace")
        self.zip([(name, b'{}\n' if name == "trace.network" else value) for name, value in self.data.items()])
        self.assert_rejected("NDJSON")

    def test_all_trace_streams_validated_empty_network_and_final_line(self):
        self.data["trace.trace"] = self.data["trace.trace"].rstrip(b"\n")
        self.data["trace.network"] = b""
        self.data["1-trace.trace"] = self.data["trace.trace"]
        self.zip()
        self.assertEqual(inspect_archive(self.source)["allocation"]["trace"]["member_count"], 2)
        self.data["1-trace.trace"] = b"{bad}\n"
        self.zip()
        self.assert_rejected("invalid NDJSON")

    def test_line_validation_across_read_boundaries(self):
        context = self.data["trace.trace"].splitlines()[0]
        self.data["trace.trace"] = context + b'\n{"type":"log","message":"' + b"x" * 200_000 + b'"}\n'
        self.zip()
        report = inspect_archive(self.source)
        self.assertEqual(next(m for m in report["members"] if m["name"] == "trace.trace")["ndjson"]["event_count"], 2)
        with self.assertRaisesRegex(ArchiveError, "line-size"):
            inspect_archive(self.source, limits=replace(Limits(), max_line_bytes=150_000))

    def test_output_replacement_aliases_and_force(self):
        self.zip()
        original = self.source.read_bytes()
        self.output.write_bytes(b"keep existing")
        with self.assertRaisesRegex(ArchiveError, "output exists"):
            repack_archive(self.source, self.output)
        self.assertEqual(self.output.read_bytes(), b"keep existing")
        repack_archive(self.source, self.output, force=True)
        self.assertTrue(inspect_archive(self.output)["verified"]["crc32"])
        for target in (self.source, self.root / "alias.zip", self.root / "hardlink.zip"):
            if target.name == "alias.zip":
                target.symlink_to(self.source)
            elif target.name == "hardlink.zip":
                target.hardlink_to(self.source)
            with self.assertRaisesRegex(ArchiveError, "input and output"):
                repack_archive(self.source, target, force=True)
        self.assertEqual(self.source.read_bytes(), original)

    def test_atomic_publication_race_and_verification_failure_cleanup(self):
        self.zip()
        self.output.write_bytes(b"existing")
        with patch("qawrench.trace_budget.archive._compare_members", side_effect=ArchiveError("injected digest mismatch")):
            with self.assertRaisesRegex(ArchiveError, "digest mismatch"):
                repack_archive(self.source, self.output, force=True)
        self.assertEqual(self.output.read_bytes(), b"existing")
        self.assertEqual(list(self.root.glob(".*.tmp")), [])
        temp = self.root / "publish.tmp"
        temp.write_bytes(b"candidate")
        with self.assertRaisesRegex(ArchiveError, "output exists"):
            _publish(temp, self.output, False)
        self.assertEqual(self.output.read_bytes(), b"existing")
        # A corrupted candidate with valid CRCs must still fail member SHA comparison.
        self.output.unlink()
        import qawrench.trace_budget.archive as module
        real_inspect = module.inspect_archive

        def different_digest(path, **kwargs):
            report = real_inspect(path, **kwargs)
            report["members"][0]["sha256"] = "0" * 64
            return report

        with patch.object(module, "inspect_archive", side_effect=different_digest):
            with self.assertRaisesRegex(ArchiveError, "verification failed"):
                repack_archive(self.source, self.output)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_source_change_detected_before_publish(self):
        self.zip()
        import qawrench.trace_budget.archive as module
        compare = module._compare_members

        def mutate(original, verified):
            compare(original, verified)
            with self.source.open("ab") as handle:
                handle.write(b"changed")

        with patch.object(module, "_compare_members", side_effect=mutate):
            with self.assertRaisesRegex(ArchiveError, "input changed"):
                repack_archive(self.source, self.output)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_repack_candidate_limit_failure_preserves_existing(self):
        self.zip()
        self.output.write_bytes(b"keep")
        # Stored input passes ratio 2, compressed output exceeds it and cannot publish.
        with self.assertRaisesRegex(ArchiveError, "compression-ratio"):
            repack_archive(self.source, self.output, force=True, limits=replace(Limits(), max_ratio=2))
        self.assertEqual(self.output.read_bytes(), b"keep")
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_cli_budget_reports_exit_codes_repack_and_dry_run(self):
        self.zip()
        report_path = self.root / "budget.json"
        inspected = self.cli("inspect", self.source, "--out", report_path, "--budget-bytes", 1)
        self.assertEqual(inspected.returncode, 2, inspected.stderr)
        report = json.loads(report_path.read_text())
        self.assertTrue(report["budget"]["exceeded"])
        self.assertEqual(json.loads(inspected.stdout), report)
        self.assertEqual(self.cli("inspect", self.source, "--out", report_path).returncode, 1)
        self.assertEqual(self.cli("inspect", self.source, "--out", report_path, "--force").returncode, 0)
        before = sorted(self.root.iterdir())
        dry = self.cli("repack", self.source, "--out", self.output, "--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertTrue(json.loads(dry.stdout)["dry_run"])
        self.assertEqual(sorted(self.root.iterdir()), before)
        repacked = self.cli("repack", self.source, "--out", self.output)
        self.assertEqual(repacked.returncode, 0, repacked.stderr)
        result = json.loads(repacked.stdout)
        self.assertEqual(result["new_bytes"], self.output.stat().st_size)
        self.assertEqual(result["ratio"], result["new_bytes"] / result["original_bytes"])
        self.assertEqual(self.cli("repack", self.source, "--out", self.output).returncode, 1)
        invalid = self.root / "invalid.zip"
        invalid.write_bytes(b"bad")
        error = self.cli("inspect", invalid)
        self.assertEqual(error.returncode, 1)
        self.assertIn("invalid ZIP", error.stderr)
        self.assertNotIn("Traceback", error.stderr)
        self.assertEqual(self.cli("inspect", self.source, "--max-ratio", "nan").returncode, 1)
        self.assertEqual(self.cli("inspect", self.source, "--budget-bytes", -1).returncode, 2)

    def test_report_cannot_overwrite_source(self):
        self.zip()
        original = self.source.read_bytes()
        with self.assertRaisesRegex(ArchiveError, "input and output"):
            write_report({"a": 1}, self.source, input_path=self.source, force=True)
        self.assertEqual(self.source.read_bytes(), original)

    def test_output_symlink_directory_and_io_failure(self):
        self.zip()
        other = self.root / "other"
        other.write_bytes(b"keep")
        self.output.symlink_to(other)
        with self.assertRaisesRegex(ArchiveError, "symlinks"):
            repack_archive(self.source, self.output, force=True)
        self.assertEqual(other.read_bytes(), b"keep")
        self.output.unlink()
        self.output.mkdir()
        with self.assertRaisesRegex(ArchiveError, "regular file"):
            repack_archive(self.source, self.output, force=True)
        self.output.rmdir()
        with patch("qawrench.trace_budget.archive._publish", side_effect=OSError("injected disk error")):
            with self.assertRaisesRegex(OSError, "disk error"):
                repack_archive(self.source, self.output)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_api_invalid_options(self):
        self.zip()
        for kwargs in ({"top": -1}, {"budget_bytes": -1}, {"budget_basis": "oops"}):
            with self.assertRaises(ArchiveError):
                inspect_archive(self.source, **kwargs)
        for level in (-1, 10, True):
            with self.assertRaises(ArchiveError):
                repack_archive(self.source, self.output, level=level)


if __name__ == "__main__":
    unittest.main()
