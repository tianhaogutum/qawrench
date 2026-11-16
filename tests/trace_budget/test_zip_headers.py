"""Local/central ZIP consistency contracts on small, independently encoded ZIPs."""

import hashlib
from pathlib import Path
import struct
import tempfile
import unittest
import zipfile
import zlib

from examples.trace_budget.generate_trace import members
from qawrench.trace_budget import ArchiveError, compare_archives, inspect_archive, repack_archive


BODY = b"local header consistency\x00\xff" * 5
SENTINEL = 0xFFFFFFFF


def synthetic_zip(*, method=8, descriptor=False, zip64=False, signed=True,
                  local=None, extra=None, descriptor_bytes=None, central_zip64=False,
                  body=BODY, body_last=False, local_version=None, central_version=None,
                  body_name="resources/body", member_flags=0):
    """Encode fixed local/central records independently of the reader under test.

    Only resources/body receives overrides. Its next-record boundary is either
    an ordinary member's local header or the central directory (body_last).
    """
    context = members()["trace.trace"].splitlines()[0] + b"\n"
    data = {"0.trace": context, body_name: body, "tail": b"preserve"}
    if body_last:
        data[body_name] = data.pop(body_name)
    records, directory = bytearray(), bytearray()
    for name, payload in data.items():
        target = name == body_name
        compression = method if target else 0
        encoded = zlib.compress(payload, wbits=-15) if compression == 8 else payload
        crc = zlib.crc32(payload)
        flags = 8 if target and descriptor else 0
        version = 45 if target and zip64 else 20
        size, stored = len(payload), len(encoded)
        fields = {"flags": flags | (member_flags if target else 0), "method": compression, "crc": 0 if flags else crc,
                  "stored": SENTINEL if target and zip64 else (0 if flags else stored),
                  "size": SENTINEL if target and zip64 else (0 if flags else size)}
        local_extra = (struct.pack("<HHQQ", 1, 16, 0 if flags else size, 0 if flags else stored)
                       if target and zip64 else b"")
        if target:
            fields.update(local or {})
            if extra is not None:
                local_extra = extra
        raw_name = name.encode("utf-8" if target and member_flags & 0x800 else "cp437")
        offset = len(records)
        records.extend(struct.pack("<4s5H3I2H", b"PK\x03\x04",
                                   local_version if target and local_version is not None else version, fields["flags"],
                                   fields["method"], 0, 0, fields["crc"], fields["stored"],
                                   fields["size"], len(raw_name), len(local_extra)))
        records.extend(raw_name + local_extra + encoded)
        if target and descriptor:
            values = struct.pack("<IQQ" if zip64 else "<III", crc, stored, size)
            dd = (b"PK\x07\x08" if signed else b"") + values
            records.extend(dd if descriptor_bytes is None else descriptor_bytes)
        central_extra = (struct.pack("<HHQQ", 1, 16, size, stored)
                         if target and central_zip64 else b"")
        directory.extend(struct.pack("<4s6H3I5H2I", b"PK\x01\x02", 45,
                                     central_version if target and central_version is not None else version, flags | (member_flags if target else 0),
                                     compression, 0, 0, crc,
                                     SENTINEL if central_extra else stored,
                                     SENTINEL if central_extra else size,
                                     len(raw_name), len(central_extra), 0, 0, 0, 0, offset))
        directory.extend(raw_name + central_extra)
    end = struct.pack("<4s4H2IH", b"PK\x05\x06", 0, 0, len(data), len(data),
                      len(directory), len(records), 0)
    return bytes(records + directory + end), data


class LocalHeaderTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / "source.zip"
        self.output = self.root / "output.zip"
        self.good = self.root / "good.zip"
        self.good.write_bytes(synthetic_zip()[0])

    def reject(self, raw, reason):
        self.source.write_bytes(raw)
        self.output.write_bytes(b"existing destination")
        for operation in (lambda: inspect_archive(self.source),
                          lambda: repack_archive(self.source, self.output, force=True),
                          lambda: compare_archives(self.good, self.source),
                          lambda: compare_archives(self.source, self.good)):
            with self.assertRaisesRegex(ArchiveError, reason + ".*resources/body"):
                operation()
            self.assertEqual(self.source.read_bytes(), raw)
            self.assertEqual(self.output.read_bytes(), b"existing destination")
            self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_local_method_flags_crc_and_sizes_must_match(self):
        for method in (0, 8):
            for field, value, reason in (("method", 99, "local ZIP method mismatch"),
                                         ("method", 8 if method == 0 else 0, "local ZIP method mismatch"),
                                         ("crc", 1, "local ZIP CRC32 mismatch"),
                                         ("crc", 0, "local ZIP CRC32 mismatch"),
                                         ("stored", 1, "local ZIP compressed size mismatch"),
                                         ("stored", 0, "local ZIP compressed size mismatch"),
                                         ("size", 1, "local ZIP uncompressed size mismatch"),
                                         ("size", 0, "local ZIP uncompressed size mismatch")):
                with self.subTest(method=method, field=field, value=value):
                    self.reject(synthetic_zip(method=method, local={field: value})[0], reason)
            for flags in (1, 2, 8, 64, 2048, 8192):
                with self.subTest(method=method, flags=flags):
                    self.reject(synthetic_zip(method=method, local={"flags": flags})[0],
                                "local ZIP flags mismatch")

    def test_valid_descriptor_and_zip64_matrix_preserves_every_payload(self):
        for method in (0, 8):
            for zip64 in (False, True):
                for descriptor, signed in ((False, False), (True, False), (True, True)):
                    for body in (b"", BODY):
                        with self.subTest(method=method, zip64=zip64, descriptor=descriptor,
                                          signed=signed, empty=not body):
                            raw, data = synthetic_zip(method=method, zip64=zip64,
                                                      descriptor=descriptor, signed=signed,
                                                      central_zip64=zip64, body=body)
                            self.source.write_bytes(raw)
                            report = inspect_archive(self.source)
                            for member in report["members"]:
                                self.assertEqual(member["sha256"], hashlib.sha256(data[member["name"]]).hexdigest())
                            result = repack_archive(self.source, self.output, force=True)
                            self.assertTrue(result["all_member_hashes_equal"])
                            with zipfile.ZipFile(self.output) as archive:
                                self.assertEqual({n: archive.read(n) for n in archive.namelist()}, data)

    def test_descriptor_placeholders_or_matching_values_only(self):
        stored = len(zlib.compress(BODY, wbits=-15))
        for zip64 in (False, True):
            with self.subTest(zip64=zip64):
                actual = {"crc": zlib.crc32(BODY)}
                if zip64:
                    extra = struct.pack("<HHQQ", 1, 16, len(BODY), stored)
                else:
                    actual.update(size=len(BODY), stored=stored)
                    extra = b""
                # Compatibility: redundant actual values are accepted, as well
                # as the zero placeholders emitted by streaming ZIP writers.
                self.source.write_bytes(synthetic_zip(descriptor=True, zip64=zip64,
                                                      local=actual, extra=extra)[0])
                self.assertTrue(inspect_archive(self.source)["verified"]["crc32"])
                for field in ("crc", "stored", "size"):
                    if zip64 and field != "crc":
                        values = (1, 0) if field == "size" else (0, 1)
                        raw = synthetic_zip(descriptor=True, zip64=True,
                                            extra=struct.pack("<HHQQ", 1, 16, *values))[0]
                    else:
                        raw = synthetic_zip(descriptor=True, zip64=zip64, local={field: 1})[0]
                    self.reject(raw, "local ZIP (CRC32|compressed size|uncompressed size) mismatch")

    def test_descriptor_crc_equal_to_optional_signature_is_unambiguous(self):
        body = bytes.fromhex("ac0a7ad5")
        self.assertEqual(zlib.crc32(body), 0x08074B50)
        for zip64 in (False, True):
            for signed in (False, True):
                with self.subTest(zip64=zip64, signed=signed):
                    self.source.write_bytes(synthetic_zip(descriptor=True, zip64=zip64,
                                                          signed=signed, body=body)[0])
                    self.assertTrue(inspect_archive(self.source)["verified"]["crc32"])
                    self.assertTrue(repack_archive(self.source, self.output, force=True)["all_member_hashes_equal"])

    def test_bad_missing_truncated_or_wrong_width_descriptors(self):
        for zip64 in (False, True):
            for signed in (False, True):
                prefix = b"PK\x07\x08" if signed else b""
                fmt = "<IQQ" if zip64 else "<III"
                values = [zlib.crc32(BODY), len(zlib.compress(BODY, wbits=-15)), len(BODY)]
                bad = [b"", prefix + struct.pack(fmt, *values)[:-1]]
                for index in range(3):
                    changed = values.copy()
                    changed[index] ^= 1
                    bad.append(prefix + struct.pack(fmt, *changed))
                bad.append(prefix + struct.pack("<III" if zip64 else "<IQQ", *values))
                for descriptor_bytes in bad:
                    for body_last in (False, True):
                        with self.subTest(zip64=zip64, signed=signed, body_last=body_last,
                                          descriptor=descriptor_bytes.hex()):
                            self.reject(synthetic_zip(descriptor=True, zip64=zip64, signed=signed,
                                                      descriptor_bytes=descriptor_bytes, body_last=body_last)[0],
                                        "invalid ZIP data descriptor")

    def test_zip64_local_extra_must_be_unique_complete_and_consistent(self):
        stored = len(zlib.compress(BODY, wbits=-15))
        valid = struct.pack("<HHQQ", 1, 16, len(BODY), stored)
        for extra in (b"", struct.pack("<HHQ", 1, 8, len(BODY)), valid + valid,
                      valid[:-1], b"\x01", struct.pack("<HHQQQ", 1, 24, len(BODY), stored, 0)):
            with self.subTest(extra=extra.hex()):
                self.reject(synthetic_zip(zip64=True, extra=extra)[0], "invalid ZIP local (extra|ZIP64)")
        for size, compressed in ((len(BODY) + 1, stored), (len(BODY), stored + 1)):
            self.reject(synthetic_zip(zip64=True, extra=struct.pack("<HHQQ", 1, 16, size, compressed))[0],
                        "local ZIP (uncompressed size|compressed size) mismatch")
        # A local ZIP64 block carries both sizes, with both 32-bit sentinels.
        self.reject(synthetic_zip(zip64=True, local={"size": len(BODY)})[0], "invalid ZIP local ZIP64")
        self.reject(synthetic_zip(extra=valid)[0], "invalid ZIP local ZIP64")
        # Unknown extras are opaque but must have complete TLV framing.
        self.source.write_bytes(synthetic_zip(extra=struct.pack("<HH3s", 0xCAFE, 3, b"raw"))[0])
        self.assertTrue(inspect_archive(self.source)["verified"]["crc32"])
        self.reject(synthetic_zip(extra=b"\xfe\xca\x02\x00x")[0], "invalid ZIP local extra")


if __name__ == "__main__":
    unittest.main()
