"""Synthetic reference-role contracts; no image decoding or browser recording."""

import hashlib
from pathlib import Path
import tempfile
import unittest
import zipfile

from examples.trace_budget.generate_trace import members, ndjson
from qawrench.trace_budget import ArchiveError, compare_archives, inspect_archive, repack_archive


GROUPS = ("screencast_screenshots", "network_bodies", "other_referenced", "shared", "unreferenced")


def fixture():
    context = members()["trace.trace"].splitlines()[0] + b"\r\n"
    trace = [
        {"type": "screencast-frame", "sha1": "frame.bin"},
        {"type": "screencast-frame", "sha1": "shared"},
        {"type": "screencast-frame", "sha1": "shared"},
        {"type": "after", "attachments": [{"sha1": "attachment"}]},
        {"type": "frame-snapshot", "snapshot": {"resourceOverrides": [{"sha1": "all"}]}},
        {"type": "screencast-frame", "sha1": "all"},
        {"type": "extension", "nested": [{"customSha1": "extension"}]},
    ]
    network = [{"type": "resource-snapshot", "snapshot": {
        "request": {"postData": {"_sha1": "request"}},
        "response": {"content": {"_sha1": name, "mimeType": "image/jpeg"}},
    }} for name in ("body.png", "shared", "all", "shared")]
    data = {"z.trace": context + ndjson(trace).replace(b"\n", b"\r\n").rstrip(b"\r\n"),
            "a.network": ndjson(network), "empty.network": b"", "resources/": b"",
            "notes/frame.bin": b"not a resource"}
    # Two identical payloads and misleading extensions must not merge/guess roles.
    # Other lengths differ so byte reconciliation also detects misallocation.
    for index, name in enumerate(("frame.bin", "shared", "all", "body.png", "request", "attachment", "extension", "unused.jpeg")):
        data[f"resources/{name}"] = (b"same opaque payload\xff" if name in ("frame.bin", "body.png")
                                     else name.encode() * (index + 1))
    roles = {"frame.bin": ["screencast_screenshots"], "body.png": ["network_bodies"],
             "request": ["network_bodies"], "attachment": ["other_referenced"],
             "extension": ["other_referenced"], "unused.jpeg": [],
             "shared": ["network_bodies", "screencast_screenshots"],
             "all": ["network_bodies", "other_referenced", "screencast_screenshots"]}
    return data, roles


def write_zip(path, data, compression=zipfile.ZIP_STORED):
    with zipfile.ZipFile(path, "w", compression=compression) as archive:
        # Resource names can precede or follow their referencing streams.
        for name, payload in reversed(list(data.items())):
            archive.writestr(name, payload)


class ResourceRoleTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source, self.after = self.root / "source.zip", self.root / "after.zip"
        self.data, self.roles = fixture()

    def assert_roles(self, report, path, expected):
        resources = {m["name"]: m for m in report["members"] if m["category"] == "resources"}
        self.assertEqual(set(resources), {"resources/" + n for n in expected})
        self.assertIn("resource_allocation", report)
        self.assertEqual(set(report["resource_allocation"]), set(GROUPS))
        names = {g: [] for g in GROUPS}
        for name, roles in expected.items():
            member = resources["resources/" + name]
            group = "shared" if len(roles) > 1 else roles[0] if roles else "unreferenced"
            self.assertEqual(member["resource_roles"], roles)
            self.assertEqual(member["resource_group"], group)
            names[group].append(member["name"])
        with zipfile.ZipFile(path) as archive:
            for group in GROUPS:
                self.assertEqual(report["resource_allocation"][group], {
                    "member_count": len(names[group]),
                    "stored_bytes": sum(archive.getinfo(n).compress_size for n in names[group]),
                    "uncompressed_bytes": sum(len(archive.read(n)) for n in names[group]),
                })
        for metric, total in report["allocation"]["resources"].items():
            self.assertEqual(sum(g[metric] for g in report["resource_allocation"].values()), total)
        for m in report["members"]:
            if m["category"] != "resources":
                self.assertNotIn("resource_roles", m)

    def test_multi_role_inventory_totals_and_exact_membership(self):
        for compression in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            with self.subTest(compression=compression):
                write_zip(self.source, self.data, compression)
                self.assert_roles(inspect_archive(self.source), self.source, self.roles)

    def test_repack_comparison_roles_and_independent_payload_parity(self):
        write_zip(self.source, self.data)
        result = repack_archive(self.source, self.after)
        before, after = inspect_archive(self.source), inspect_archive(self.after)
        self.assert_roles(after, self.after, self.roles)
        comparison = compare_archives(self.source, self.after)
        self.assertEqual(result["comparison"], comparison)
        self.assertTrue(result["all_member_hashes_equal"])
        self.assertEqual(comparison["loss_ledger"]["members"], [])
        for group in GROUPS:
            change = comparison["resource_allocation"][group]
            self.assertEqual(change["before"], before["resource_allocation"][group])
            self.assertEqual(change["after"], after["resource_allocation"][group])
            for metric in change["delta"]:
                self.assertEqual(change["delta"][metric], change["after"][metric] - change["before"][metric])
        with zipfile.ZipFile(self.after) as archive:
            self.assertEqual(set(archive.namelist()), set(self.data))
            for retained in comparison["retained_payloads"]:
                name = retained["name"]
                self.assertEqual(archive.read(name), self.data[name])
                self.assertEqual(retained["after_sha256"], hashlib.sha256(self.data[name]).hexdigest())
                self.assertEqual(retained["before_sha256"], retained["after_sha256"])
                if name.startswith("resources/") and name != "resources/":
                    self.assertEqual(retained["before_resource_roles"], self.roles[name[10:]])
                    self.assertEqual(retained["after_resource_roles"], self.roles[name[10:]])

    def test_opaque_nonmatches_and_unknown_reference_locations(self):
        context = self.data["z.trace"].splitlines()[0] + b"\n"
        self.data = {"trace.trace": context, "resources/keep.jpeg": b"image?", "resources/dir/": b"",
                     "resources/nested/keep.jpeg": b"nested", "notes/keep.jpeg": b"outside"}
        events = []
        for value in (None, False, 1, [], {}, "", "missing", "Keep.jpeg", "resources/keep.jpeg",
                      "../keep.jpeg", "./keep.jpeg", "/keep.jpeg", "nested/../keep.jpeg", "%6beep.jpeg", "dir/"):
            events.extend([{"type": "screencast-frame", "sha1": value},
                           {"type": "resource-snapshot", "snapshot": {"response": {"content": {"_sha1": value}}}}])
        for value in (None, [], "keep.jpeg", 1):
            events.append({"type": "resource-snapshot", "snapshot": {"request": value, "response": {"content": value}}})
        events += [{"type": "extension", "text": "keep.jpeg", "_file": "keep.jpeg"},
                   {"type": "screencast-frame", "sha1": {"nested": "keep.jpeg"}}]
        self.data["trace.trace"] += ndjson(events)
        write_zip(self.source, self.data)
        self.assert_roles(inspect_archive(self.source), self.source, {"keep.jpeg": [], "nested/keep.jpeg": []})
        # A known role requires its exact slot. Other keys are conservatively attributed.
        self.data["trace.trace"] += ndjson([
            {"type": "screencast-frame", "_sha1": "keep.jpeg"},
            {"type": "extension", "nested": [{"sha1": "nested/keep.jpeg"}]},
            {"type": "resource-snapshot", "snapshot": {"response": {"content": {"sha1": "keep.jpeg"}}}},
        ])
        write_zip(self.source, self.data)
        self.assert_roles(inspect_archive(self.source), self.source,
                          {"keep.jpeg": ["other_referenced"], "nested/keep.jpeg": ["other_referenced"]})

    def test_stream_removal_reclassifies_retained_resources_and_audits_losses(self):
        write_zip(self.source, self.data)
        data = {n: p for n, p in self.data.items() if n not in ("a.network", "resources/attachment")}
        write_zip(self.after, data, zipfile.ZIP_DEFLATED)
        with self.assertRaisesRegex(ArchiveError, "removals require"):
            compare_archives(self.source, self.after)
        report = compare_archives(self.source, self.after, allow_removals=True)
        expected = {n: [r for r in roles if r != "network_bodies"]
                    for n, roles in self.roles.items() if n != "attachment"}
        self.assert_roles(inspect_archive(self.after), self.after, expected)
        for phase in ("before", "after", "delta"):
            for metric in ("member_count", "stored_bytes", "uncompressed_bytes"):
                self.assertEqual(sum(g[phase][metric] for g in report["resource_allocation"].values()),
                                 report["allocation"]["resources"][phase][metric])
        retained = next(m for m in report["retained_payloads"] if m["name"] == "resources/shared")
        self.assertEqual(retained["before_resource_roles"], self.roles["shared"])
        self.assertEqual(retained["after_resource_roles"], ["screencast_screenshots"])
        loss = next(m for m in report["loss_ledger"]["members"] if m["name"] == "resources/attachment")
        self.assertEqual(loss["resource_roles"], ["other_referenced"])
        self.assertEqual(loss["resource_group"], "other_referenced")
        self.assertTrue(report["all_retained_hashes_equal"])

    def test_roles_never_relax_retained_parity_or_accept_additions(self):
        write_zip(self.source, self.data)
        for name, payload in (("resources/shared", b"changed resource"),
                              ("z.trace", self.data["z.trace"].replace(b'"sha1":"shared"', b'"sha1":"frame.bin"')),
                              ("resources/added", b"new")):
            with self.subTest(name=name):
                write_zip(self.after, {**self.data, name: payload})
                for allow in (False, True):
                    with self.assertRaisesRegex(ArchiveError, "retained payload differs|added members"):
                        compare_archives(self.source, self.after, allow_removals=allow)

    def test_multistream_chunk_boundaries_and_no_resources(self):
        context = self.data["z.trace"].splitlines()[0] + b"\n"
        # Cross the bounded reader chunk with reference fields on the same line.
        self.data["secondary.trace"] = context + ndjson([
            {"type": "resource-snapshot", "padding": "x" * 140_000,
             "snapshot": {"request": {"postData": {"_sha1": "frame.bin"}}}},
        ])
        write_zip(self.source, self.data)
        expected = {**self.roles, "frame.bin": ["network_bodies", "screencast_screenshots"]}
        self.assert_roles(inspect_archive(self.source), self.source, expected)
        write_zip(self.source, {"trace.trace": context})
        self.assert_roles(inspect_archive(self.source), self.source, {})


if __name__ == "__main__":
    unittest.main()
