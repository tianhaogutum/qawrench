"""Synthetic before/after audits, including explicit and independently checked loss."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

from examples.trace_budget.generate_trace import members, ndjson
import qawrench.trace_budget as api


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.before = self.root / "before.zip"
        self.after = self.root / "after.zip"
        context = members()["trace.trace"].splitlines()[0]
        # Exact byte accounting must preserve CRLF, Unicode, and no final newline.
        self.context = context + b"\r\n"
        self.log = ndjson([{"type": "extension-event", "message": "保留"}]).rstrip(b"\n")
        self.data = {"trace.trace": self.context + self.log,
                     "trace.network": ndjson([{"type": "resource-snapshot", "snapshot": {}}]),
                     "resources/body.trace": b"opaque\xff" * 100,
                     "empty/": b"", "notes/über.txt": b"keep exact bytes"}
        self.write(self.before, self.data)

    def write(self, path, data, compression=zipfile.ZIP_STORED):
        with zipfile.ZipFile(path, "w", compression=compression) as archive:
            for name, payload in data.items():
                archive.writestr(name, payload)

    def cli(self, *args):
        return subprocess.run([sys.executable, "-m", "qawrench.trace_budget", *map(str, args)],
                              capture_output=True, text=True, timeout=30)

    def test_comparison_repack_category_deltas_and_exact_retained_hashes(self):
        result = api.repack_archive(self.before, self.after)
        comparison = result["comparison"]
        self.assertEqual(comparison, api.compare_archives(self.before, self.after))
        self.assertEqual(comparison["loss_ledger"]["members"], [])
        self.assertEqual(comparison["loss_ledger"]["removed_member_count"], 0)
        self.assertTrue(comparison["all_retained_hashes_equal"])
        self.assertEqual(comparison["retained_member_count"], len(self.data))
        category_names = {"trace": ["trace.trace"], "network": ["trace.network"],
                          "resources": ["resources/body.trace"], "other": ["empty/", "notes/über.txt"]}
        with zipfile.ZipFile(self.before) as old, zipfile.ZipFile(self.after) as new:
            for category, names in category_names.items():
                group = comparison["allocation"][category]
                for phase, archive in (("before", old), ("after", new)):
                    self.assertEqual(group[phase], {
                        "member_count": len(names),
                        "stored_bytes": sum(archive.getinfo(n).compress_size for n in names),
                        "uncompressed_bytes": sum(len(self.data[n]) for n in names)})
                for key in group["before"]:
                    self.assertEqual(group["delta"][key], group["after"][key] - group["before"][key])
        for retained in comparison["retained_payloads"]:
            digest = hashlib.sha256(self.data[retained["name"]]).hexdigest()
            self.assertEqual(retained["before_sha256"], digest)
            self.assertEqual(retained["after_sha256"], digest)
        events = comparison["event_allocation"]["trace"]
        for kind, payload in (("context-options", self.context), ("extension-event", self.log)):
            self.assertEqual(events[kind]["before"], {"event_count": 1, "uncompressed_bytes": len(payload)})
            self.assertEqual(events[kind]["delta"], {"event_count": 0, "uncompressed_bytes": 0})
        totals = comparison["totals"]
        self.assertEqual(totals["before"]["archive_bytes"], self.before.stat().st_size)
        self.assertEqual(totals["after"]["archive_bytes"], self.after.stat().st_size)
        self.assertEqual(sum(g["delta"]["stored_bytes"] for g in comparison["allocation"].values())
                         + totals["delta"]["zip_overhead_bytes"], totals["delta"]["archive_bytes"])

    def test_comparison_optional_removals_have_exact_loss_and_event_ledger(self):
        removed_trace = self.context + ndjson([{"type": "log", "message": "removed"}])
        self.data["secondary.trace"] = removed_trace
        self.write(self.before, self.data)
        removed = {"resources/body.trace", "secondary.trace", "empty/"}
        self.write(self.after, {n: p for n, p in self.data.items() if n not in removed}, zipfile.ZIP_DEFLATED)
        with self.assertRaisesRegex(api.ArchiveError, "removals require.*allow_removals"):
            api.compare_archives(self.before, self.after)
        result = api.compare_archives(self.before, self.after, allow_removals=True)
        loss = result["loss_ledger"]
        self.assertTrue(loss["removals_allowed"])
        self.assertEqual(loss["removed_member_count"], 3)
        self.assertEqual(loss["removed_uncompressed_bytes"], sum(len(self.data[n]) for n in removed))
        self.assertEqual([m["name"] for m in loss["members"]], sorted(removed))
        for member in loss["members"]:
            self.assertEqual(member["sha256"], hashlib.sha256(self.data[member["name"]]).hexdigest())
        event = result["event_allocation"]["trace"]["log"]
        self.assertEqual(event["after"], {"event_count": 0, "uncompressed_bytes": 0})
        self.assertEqual(event["delta"]["event_count"], -1)
        self.assertEqual(loss["members"][1]["name"], "resources/body.trace")
        self.assertEqual(loss["members"][1]["category"], "resources")
        stream_loss = next(m for m in loss["members"] if m["name"] == "secondary.trace")
        self.assertEqual(stream_loss["ndjson"]["event_types"], {"context-options": 1, "log": 1})
        self.assertEqual(stream_loss["ndjson"]["event_uncompressed_bytes"],
                         {"context-options": len(self.context), "log": len(removed_trace) - len(self.context)})
        context_counts = result["event_allocation"]["trace"]["context-options"]
        self.assertEqual(context_counts["before"]["event_count"], 2)
        self.assertEqual(context_counts["after"]["event_count"], 1)
        self.assertEqual(result["allocation"]["other"]["delta"]["member_count"], -1)

    def test_comparison_rejects_changed_retained_payloads_and_additions(self):
        for name, payload in (("notes/über.txt", b"KEEP exact bytes"),
                              ("trace.trace", self.context + self.log + b"\n"),
                              ("extra", b"new")):
            with self.subTest(name=name):
                changed = {**self.data, name: payload}
                self.write(self.after, changed)
                with self.assertRaisesRegex(api.ArchiveError, "retained payload differs|added members"):
                    api.compare_archives(self.before, self.after, allow_removals=True)
        self.write(self.after, self.data)
        with self.assertRaisesRegex(api.ArchiveError, "allow_removals must be a boolean"):
            api.compare_archives(self.before, self.after, allow_removals=1)
        # Equality at all budget boundaries passes; one byte less fails.
        for basis, measured in (("archive", self.after.stat().st_size),
                                ("stored", sum(map(len, self.data.values()))),
                                ("uncompressed", sum(map(len, self.data.values())))):
            self.assertFalse(api.compare_archives(self.before, self.after, budget_basis=basis,
                                                budget_bytes=measured)["budget"]["exceeded"])
            self.assertTrue(api.compare_archives(self.before, self.after, budget_basis=basis,
                                               budget_bytes=measured - 1)["budget"]["exceeded"])

    def test_comparison_cli_budget_failure_outputs_and_both_inputs_protected(self):
        self.write(self.after, {n: p for n, p in self.data.items() if n != "resources/body.trace"})
        out = self.root / "comparison.json"
        out.write_bytes(b"keep report")
        args = ("compare", self.before, self.after, "--out", out, "--force")
        failed = self.cli(*args)
        self.assertEqual(failed.returncode, 1, failed.stderr)
        self.assertEqual(failed.stdout, "")
        self.assertEqual(out.read_bytes(), b"keep report")
        passed = self.cli(*args, "--allow-removals", "--budget-bytes", 0)
        self.assertEqual(passed.returncode, 2, passed.stderr)
        report = json.loads(passed.stdout)
        self.assertEqual(report, json.loads(out.read_text()))
        self.assertTrue(report["budget"]["exceeded"])
        self.assertEqual(report["budget"]["measured_bytes"], self.after.stat().st_size)
        self.assertEqual(report["budget"]["before_measured_bytes"], self.before.stat().st_size)
        self.assertTrue(report["budget"]["before_exceeded"])
        for source in (self.before, self.after):
            for alias_kind in ("direct", "hardlink", "symlink"):
                alias = self.root / f"{source.stem}-{alias_kind}"
                if alias_kind == "direct":
                    alias = source
                elif alias_kind == "hardlink":
                    alias.hardlink_to(source)
                else:
                    alias.symlink_to(source)
                original = source.read_bytes()
                failure = self.cli("compare", self.before, self.after, "--allow-removals",
                                   "--out", alias, "--force")
                self.assertEqual(failure.returncode, 1, failure.stderr)
                self.assertRegex(failure.stderr, "input and output|output symlinks")
                self.assertEqual(source.read_bytes(), original)
        # Invalid candidates still undergo the same bounded archive validation.
        self.after.write_bytes(b"not a zip")
        failure = self.cli(*args, "--allow-removals")
        self.assertEqual(failure.returncode, 1)
        self.assertIn("invalid ZIP", failure.stderr)
        self.assertEqual(out.read_text(), passed.stdout)
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_comparison_line_bytes_across_chunks_and_empty_network(self):
        log = ndjson([{"type": "log", "message": "x" * 300_000}])
        self.data["trace.trace"] = self.context + log + self.log
        self.data["trace.network"] = b""
        self.write(self.before, self.data)
        self.write(self.after, self.data, zipfile.ZIP_DEFLATED)
        report = api.compare_archives(self.before, self.after)
        self.assertEqual(report["event_allocation"]["network"], {})
        events = report["event_allocation"]["trace"]
        self.assertEqual(events["log"]["before"]["uncompressed_bytes"], len(log))
        self.assertEqual(sum(e["before"]["uncompressed_bytes"] for e in events.values()),
                         len(self.data["trace.trace"]))
        self.assertEqual(sum(e["after"]["uncompressed_bytes"] for e in events.values()),
                         len(self.data["trace.trace"]))
        budget = api.compare_archives(self.before, self.after,
                                      budget_bytes=self.after.stat().st_size)["budget"]
        self.assertTrue(budget["before_exceeded"])
        self.assertFalse(budget["exceeded"])


if __name__ == "__main__":
    unittest.main()
