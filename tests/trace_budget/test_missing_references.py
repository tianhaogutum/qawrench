"""Bounded known-slot diagnostics remain separate from payload allocation/parity."""

import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch
import zipfile

from examples.trace_budget.generate_trace import members, ndjson
from qawrench.trace_budget import ArchiveError, compare_archives, inspect_archive, repack_archive
from qawrench.trace_budget.cli import main


KEY = "resource_reference_diagnostics"
ROLES = ("screencast_screenshots", "network_bodies")


def body(request=None, response=None):
    return {"type": "resource-snapshot", "snapshot": {
        "request": {"postData": {"_sha1": request}},
        "response": {"content": {"_sha1": response}},
    }}


def fixture():
    context = members()["trace.trace"].splitlines()[0] + b"\r\n"
    return {
        "z.trace": context + ndjson([
            {"type": "screencast-frame", "sha1": "missing.jpeg"},
            {"type": "screencast-frame", "sha1": "missing.jpeg"},
            {"type": "screencast-frame", "sha1": "present-body"},
            body("absent-request", "present-body"),
        ]).replace(b"\n", b"\r\n").rstrip(b"\r\n"),
        "a.network": ndjson([body("absent-request", "present-body"),
                              body("present-body", "missing.jpeg")]),
        "secondary.trace": context + ndjson([{"type": "screencast-frame", "sha1": "missing.jpeg"}]),
        "empty.network": b"", "resources/present-body": b"opaque payload\xff",
        "resources/unused": b"preserve this too", "resources/dir/": b"",
    }


def write_zip(path, data):
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, payload in reversed(list(data.items())):
            archive.writestr(name, payload)


class MissingReferenceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source, self.after = self.root / "source.zip", self.root / "after.zip"
        self.data = fixture()
        write_zip(self.source, self.data)

    def check_counts(self, diagnostics, screenshots, network):
        self.assertEqual(diagnostics["missing_occurrences"], screenshots + network)
        self.assertEqual(diagnostics["missing_occurrences_by_role"], dict(zip(ROLES, (screenshots, network))))
        self.assertEqual(diagnostics["sample_limit"], 20)
        self.assertEqual(diagnostics["sample_text_limit"], 160)
        self.assertEqual(diagnostics["sample_omitted_occurrences"],
                         screenshots + network - len(diagnostics["samples"]))

    def test_confirmed_missing_reference_occurrences(self):
        report = inspect_archive(self.source, top=0)
        self.assertIn(KEY, report, "known unresolved references need bounded diagnostics")
        diagnostics = report[KEY]
        self.check_counts(diagnostics, 3, 3)
        self.assertEqual([(s["stream_prefix"], s["line"], s["reference_prefix"]) for s in diagnostics["samples"]], [
            ("a.network", 1, "absent-request"), ("a.network", 2, "missing.jpeg"),
            ("secondary.trace", 2, "missing.jpeg"), ("z.trace", 2, "missing.jpeg"),
            ("z.trace", 3, "missing.jpeg"), ("z.trace", 5, "absent-request"),
        ])
        self.assertEqual(diagnostics["samples"][0], {
            "role": "network_bodies", "slot": "snapshot.request.postData._sha1",
            "stream_prefix": "a.network", "stream_truncated": False, "line": 1,
            "reference_prefix": "absent-request", "reference_truncated": False,
        })
        self.assertEqual(report["allocation"]["resources"], {
            "member_count": 2, "stored_bytes": 32, "uncompressed_bytes": 32})
        self.assertEqual(report["resource_allocation"]["shared"]["member_count"], 1)
        self.assertEqual(report["resource_allocation"]["unreferenced"]["member_count"], 1)
        for metric, total in report["allocation"]["resources"].items():
            self.assertEqual(sum(g[metric] for g in report["resource_allocation"].values()), total)

    def test_exact_matching_malformed_shapes_and_unknown_slots(self):
        context = self.data["z.trace"].splitlines()[0] + b"\n"
        events = []
        for value in (None, False, 1, [], {}, ""):
            events += [{"type": "screencast-frame", "sha1": value}, body(value, value)]
        for value in (None, [], "missing", 1):
            events += [{"type": "resource-snapshot", "snapshot": {"request": value, "response": {"content": value}}},
                       {"type": "screencast-frame", "sha1": [{"sha1": "missing"}]}]
        events += [{"type": "resource-snapshot", "snapshot": []},
                   {"type": "extension", "nested": [{"sha1": "missing"}, {"customSha1": "missing"}]},
                   {"type": "screencast-frame", "_sha1": "missing"},
                   {"type": "extension", "sha1": "present-body"}]
        data = {"only.trace": context + ndjson(events), "resources/present-body": b"body",
                "notes/present-body": b"outside", "resources/dir/": b""}
        write_zip(self.source, data)
        self.check_counts(inspect_archive(self.source)[KEY], 0, 0)
        # Paths/case/whitespace are exact strings, not invalid envelopes or normalized matches.
        nonmatches = ["Present-body", "resources/present-body", "./present-body", "../present-body",
                      "/present-body", "%70resent-body", "dir/", " "]
        data["only.trace"] += ndjson([body(v, v) for v in nonmatches] + [body("present-body", "present-body")])
        write_zip(self.source, data)
        report = inspect_archive(self.source)
        self.check_counts(report[KEY], 0, 16)
        self.assertEqual(next(m for m in report["members"] if m["name"] == "resources/present-body")["resource_roles"], ["network_bodies", "other_referenced"])
        # With no resources every valid known-slot string is missing, still zero allocation.
        write_zip(self.source, {"only.trace": context + ndjson([body("x", "x")])})
        report = inspect_archive(self.source)
        self.check_counts(report[KEY], 0, 2)
        self.assertEqual(report["allocation"]["resources"]["member_count"], 0)

    def test_sample_cap_long_identifiers_repeats_and_stream_order(self):
        context = self.data["z.trace"].splitlines()[0] + b"\n"
        long_name = "a" * 200 + ".trace"
        values = ["保" * 161, "x" * 160, "x" * 159] + [f"missing-{i}" for i in range(30)]
        data = {long_name: context + ndjson([body(v, v) for v in values]),
                "z.trace": context + ndjson([{"type": "screencast-frame", "sha1": "repeat"}] * 2000)}
        write_zip(self.source, data)
        report = inspect_archive(self.source)[KEY]
        self.check_counts(report, 2000, 66)
        self.assertEqual(len(report["samples"]), 20)
        for sample in report["samples"]:
            self.assertEqual(len(sample["stream_prefix"]), 160)
            self.assertTrue(sample["stream_truncated"])
            self.assertLessEqual(len(sample["reference_prefix"]), 160)
        self.assertEqual(report["samples"][0]["reference_prefix"], "保" * 160)
        self.assertTrue(report["samples"][0]["reference_truncated"])
        self.assertFalse(report["samples"][2]["reference_truncated"])
        self.assertFalse(report["samples"][4]["reference_truncated"])
        self.assertEqual([s["slot"] for s in report["samples"][:2]],
                         ["snapshot.request.postData._sha1", "snapshot.response.content._sha1"])
        # ZIP physical order must not affect deterministic sample ordering.
        with zipfile.ZipFile(self.after, "w") as archive:
            for name, payload in data.items():
                archive.writestr(name, payload)
        self.assertEqual(inspect_archive(self.after)[KEY], report)

    def test_sample_limit_edges_and_reference_across_reader_chunks(self):
        context = self.data["z.trace"].splitlines()[0] + b"\n"
        for count in (0, 1, 19, 20, 21):
            with self.subTest(count=count):
                events = [{"type": "screencast-frame", "padding": "x" * 140_000,
                           "sha1": "missing"}] * count
                write_zip(self.source, {"trace.trace": context + ndjson(events)})
                diagnostics = inspect_archive(self.source)[KEY]
                self.check_counts(diagnostics, count, 0)
                self.assertEqual(len(diagnostics["samples"]), min(count, 20))
                self.assertEqual(diagnostics["sample_omitted_occurrences"], max(count - 20, 0))

    def test_repack_and_external_resource_or_stream_removals(self):
        before = inspect_archive(self.source)[KEY]
        result = repack_archive(self.source, self.after)
        comparison = compare_archives(self.source, self.after)
        self.assertEqual(result["comparison"], comparison)
        self.assertEqual(comparison[KEY]["before"], before)
        self.assertEqual(comparison[KEY]["after"], before)
        self.assertEqual(comparison[KEY]["delta"], {
            "missing_occurrences": 0, "missing_occurrences_by_role": dict.fromkeys(ROLES, 0)})
        with zipfile.ZipFile(self.after) as archive:
            self.assertEqual(set(archive.namelist()), set(self.data))
            for name, payload in self.data.items():
                self.assertEqual(archive.read(name), payload)
        for removed, counts, delta in [("resources/present-body", (4, 6), 4),
                                        ("a.network", (3, 1), -2)]:
            with self.subTest(removed=removed):
                write_zip(self.after, {n: p for n, p in self.data.items() if n != removed})
                with self.assertRaisesRegex(ArchiveError, "removals require"):
                    compare_archives(self.source, self.after)
                audit = compare_archives(self.source, self.after, allow_removals=True)
                self.check_counts(audit[KEY]["after"], *counts)
                self.assertEqual(audit[KEY]["delta"]["missing_occurrences"], delta)
                self.assertEqual(audit[KEY]["delta"]["missing_occurrences_by_role"],
                                 dict(zip(ROLES, (counts[0] - 3, counts[1] - 3))))
                self.assertEqual(audit["loss_ledger"]["members"][0]["name"], removed)
                self.assertTrue(audit["all_retained_hashes_equal"])
                for retained in audit["retained_payloads"]:
                    digest = hashlib.sha256(self.data[retained["name"]]).hexdigest()
                    self.assertEqual(retained["before_sha256"], digest)
                    self.assertEqual(retained["after_sha256"], digest)

    def test_failures_preserve_outputs_and_exact_retained_parity(self):
        output = self.root / "report.json"
        for name, payload in [("z.trace", self.data["z.trace"].replace(b"missing.jpeg", b"another.jpeg")),
                              ("resources/present-body", b"changed"), ("added", b"addition")]:
            write_zip(self.after, {**self.data, name: payload})
            for allow in (False, True):
                output.write_bytes(b"keep report")
                stdout, stderr = io.StringIO(), io.StringIO()
                args = ["compare", str(self.source), str(self.after), "--out", str(output), "--force"]
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = main(args + (["--allow-removals"] if allow else []))
                self.assertEqual(code, 1, stderr.getvalue())
                self.assertEqual(stdout.getvalue(), "")
                self.assertRegex(stderr.getvalue(), "retained payload differs|added members")
                self.assertEqual(output.read_bytes(), b"keep report")
        # Corrupt an actual candidate after it was written: temporary and output guarantees still apply.
        import qawrench.trace_budget.archive as module
        real_inspect = module.inspect_archive
        def corrupt_candidate(path, **kwargs):
            with zipfile.ZipFile(path, "a") as archive:
                archive.writestr("new-member", b"unexpected")
            return real_inspect(path, **kwargs)
        self.after.write_bytes(b"keep destination")
        with patch.object(module, "inspect_archive", side_effect=corrupt_candidate):
            with self.assertRaisesRegex(ArchiveError, "repack verification failed"):
                repack_archive(self.source, self.after, force=True)
        self.assertEqual(self.after.read_bytes(), b"keep destination")
        self.assertEqual(list(self.root.glob(".*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
