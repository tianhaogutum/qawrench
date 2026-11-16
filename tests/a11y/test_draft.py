"""Draft from a validated delta identity, never from a manually copied target/count."""
from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import qawrench.a11y as bridge
from qawrench.a11y.core import POLICY, canonical, fingerprint

ROOT = Path(__file__).resolve().parents[2]
REVIEW = dict(owner="fixture-team", reason="Track synthetic replacement", expires="9999-12-31")
DEFAULT = object()


def nested(copies=1):
    report = json.loads((ROOT / "examples/a11y/axe-nested.json").read_text())
    report["violations"][0]["nodes"] *= copies
    return bridge.make_baseline(report, "axe")


def delta():
    return bridge.compare(nested(), nested(3), fail_on="serious")


def existing_reviews():
    # Preserve literal URL spelling, review text, expired/unmatched entries and budgets.
    policy = json.loads((ROOT / "examples/a11y/reviewed-exceptions.json").read_text())
    policy["exceptions"][0].update(page="HTTPS://FIXTURE.TEST:443", owner=" team with spaces ",
                                   reason=" keep this reason verbatim ", maxOccurrences=7)
    return policy


class DraftTests(unittest.TestCase):
    def draft(self, document=DEFAULT, identity=None, **changes):
        document = delta() if document is DEFAULT else document
        identity = nested()["findings"][0]["fingerprint"] if identity is None else identity
        return bridge.draft_exception(document, identity, **{**REVIEW, **changes})

    def test_nested_identity_copies_full_target_and_total_current_budget(self):
        document = delta()
        original = deepcopy(document)
        result = self.draft(document)
        finding = document["new"][0]
        self.assertEqual(result, {"schemaVersion": 1, "kind": "a11y-reviewed-exceptions",
                                 "identityPolicy": POLICY, "exceptions": [
            {**{k: finding[k] for k in ("engine", "page", "rule", "target")},
             **REVIEW, "maxOccurrences": 3}]})
        self.assertEqual(document, original)
        self.assertEqual(bridge.validate_exceptions(result), result)
        reviewed = bridge.compare(nested(), nested(3), fail_on="serious", exceptions=result)
        self.assertTrue(reviewed["gate"]["passed"])
        self.assertEqual(reviewed["exceptions"]["entries"][0]["exceptedNewOccurrences"], 2)
        self.assertFalse(bridge.compare(nested(), nested(4), fail_on="serious", exceptions=result)["gate"]["passed"])
        result["exceptions"][0]["target"][1][0] = "changed"
        self.assertEqual(document, original)

    def test_unchanged_identity_and_reviewed_delta_replay(self):
        unchanged = bridge.compare(nested(), nested())
        self.assertEqual(self.draft(unchanged)["exceptions"][0]["maxOccurrences"], 1)
        policy = self.draft()
        # Historical evaluation must be replayed, not recomputed at today's date.
        historical = bridge.compare(nested(), nested(3), exceptions=policy,
                                    evaluation_date=date(9999, 12, 31), fail_on="serious")
        self.assertEqual(self.draft(historical), policy)
        self.assertEqual(historical["exceptions"]["counts"]["expired"], 1)

    def test_existing_reviews_union_preserves_entries_and_inputs(self):
        document, existing = delta(), existing_reviews()
        originals = deepcopy((document, existing))
        result = self.draft(document, existing_reviews=existing)
        new_entry = self.draft(document)["exceptions"][0]
        self.assertEqual(result["exceptions"], sorted([*existing["exceptions"], new_entry], key=canonical))
        self.assertEqual((document, existing), originals)
        bridge.validate_exceptions(result)
        reversed_reviews = {**existing, "exceptions": list(reversed(existing["exceptions"]))}
        self.assertEqual(self.draft(document, existing_reviews=reversed_reviews), result)
        self.assertEqual(self.draft(document, existing_reviews={**existing, "exceptions": []}),
                         self.draft(document))
        applied = bridge.compare(nested(), nested(3), exceptions=result, fail_on="serious",
                                 evaluation_date=date(2026, 11, 11))
        self.assertTrue(applied["gate"]["passed"])
        self.assertEqual(applied["exceptions"]["counts"],
                         {"active": 1, "expired": 1, "unmatched": 2, "exceptedNew": 2})
        self.assertFalse(bridge.compare(nested(), nested(4), exceptions=result,
                                       fail_on="serious")["gate"]["passed"])
        # Returned nested targets and retained entries must be independent copies.
        result["exceptions"].remove(new_entry)
        result["exceptions"][0]["target"].append("#mutated")
        self.assertEqual((document, existing), originals)

    def test_existing_reviews_reject_duplicate_normalized_identity_or_malformed_document(self):
        own = self.draft()
        duplicate = deepcopy(own["exceptions"][0])
        duplicate.update(page="HTTPS://FIXTURE.TEST:443/checkout", expires="2000-01-01",
                         maxOccurrences=1, owner="old-team", reason="do not renew")
        # Use an equivalent spelling of the actual nested fixture's URL.
        duplicate["page"] = own["exceptions"][0]["page"].replace("https://fixture.test", "HTTPS://FIXTURE.TEST:443")
        for existing in (own, {**own, "exceptions": [duplicate]}):
            with self.subTest(existing=existing), self.assertRaisesRegex(bridge.BridgeError, "duplicate"):
                self.draft(existing_reviews=existing)
        duplicate = deepcopy(existing_reviews()["exceptions"][0])
        duplicate["page"] = "https://fixture.test/"
        invalid = [None, [], {}, {**own, "schemaVersion": True}, {**own, "extra": 1},
                   {**own, "exceptions": [*existing_reviews()["exceptions"], duplicate]}]
        for field, value in (("maxOccurrences", 0), ("maxOccurrences", True), ("expires", "2026-02-30"),
                             ("owner", " "), ("target", []), ("engine", "unknown")):
            entry = deepcopy(own["exceptions"][0]); entry[field] = value
            invalid.append({**own, "exceptions": [entry]})
        for existing in invalid:
            with self.subTest(existing=existing), self.assertRaises(bridge.BridgeError):
                self.draft(existing_reviews=existing)

    def test_pa11y_page_level_targets_and_runner_namespaces(self):
        report = json.loads((ROOT / "examples/a11y/pa11y.json").read_text())
        issues = next(v for v in report["results"].values() if v)
        issues[0]["selector"] = ""
        issues.append({**deepcopy(issues[0]), "runner": "axe"})
        report["errors"] += 1
        current = bridge.make_baseline(report, "pa11y-ci", input_version="4.0.0")
        document = bridge.compare(current, current)
        for f in current["findings"]:
            with self.subTest(engine=f["engine"], target=f["target"]):
                entry = self.draft(document, f["fingerprint"])["exceptions"][0]
                self.assertEqual((entry["engine"], entry["target"], entry["maxOccurrences"]),
                                 (f["engine"], f["target"], 1))
        # Empty selectors remain distinct across namespaces when unioned in one file.
        policy = {**existing_reviews(), "exceptions": []}
        for f in current["findings"]:
            policy = self.draft(document, f["fingerprint"], existing_reviews=policy)
        self.assertEqual(len(policy["exceptions"]), len(current["findings"]))
        self.assertEqual(sum(e["target"] == "" for e in policy["exceptions"]), 2)

    def test_fixed_only_unknown_or_nonexact_identity_rejected(self):
        before = nested()
        report = deepcopy(before["source"]["report"])
        report["violations"] = []
        fixed = bridge.compare(before, bridge.make_baseline(report, "axe"))
        for document, identity in [(fixed, None), (delta(), "0" * 64),
                                   (delta(), "a" * 12), (delta(), []), (delta(), "F" * 64)]:
            with self.subTest(identity=identity), self.assertRaises(bridge.BridgeError):
                self.draft(document, identity)

    def test_explicit_review_fields_follow_existing_schema(self):
        for change in ({"owner": " "}, {"reason": ""}, {"expires": "2026-02-30"},
                       {"expires": "20261201"}, {"expires": "2026-12-01T00:00:00Z"},
                       {"expires": None}, {"owner": object()}, {"reason": object()},
                       {"expires": object()}):
            with self.subTest(change=change), self.assertRaises(bridge.BridgeError):
                self.draft(**change)
        # Drafts can record an expired review, which still waives nothing.
        self.assertEqual(self.draft(expires="2000-01-01")["exceptions"][0]["expires"], "2000-01-01")

    def test_inconsistent_delta_or_native_sources_rejected(self):
        bad = [None, {}, [], {**delta(), "schemaVersion": True},
               {**delta(), "schemaVersion": 2}, {**delta(), "kind": "a11y-migration-diagnostics"},
               {**delta(), "identityPolicy": "other"}]
        for path, value in [(("counts", "current"), 99), (("new", 0, "target"), ["#fake"]),
                            (("new", 0, "fingerprint"), "0" * 64),
                            (("unchanged", 0, "current", "rule"), "fake"),
                            (("sources", "current", "report", "incomplete"), [{}]),
                            (("sources", "current", "inputVersion"), "unknown"),
                            (("gate", "failOn"), "unknown"), (("gate", "passed"), True),
                            (("fixed",), [nested()["findings"][0]]), (("pages",), [])]:
            document = delta()
            target = document
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            bad.append(document)
        bad.append({**delta(), "sources": {}})
        reviewed = bridge.compare(nested(), nested(3), exceptions=self.draft())
        for path, value in [(("evaluatedOn",), "invalid"), (("entries",), [None]),
                            (("entries", 0, "currentOccurrences"), 99)]:
            document = deepcopy(reviewed)
            target = document["exceptions"]
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            bad.append(document)
        for document in bad:
            with self.subTest(document=document), self.assertRaises(bridge.BridgeError):
                self.draft(document)


class DraftCliTests(unittest.TestCase):
    def test_cli_existing_reviews_dry_run_and_union_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            source, existing, out = [Path(directory) / name for name in ("delta.json", "existing.json", "out.json")]
            source.write_text(json.dumps(delta()))
            existing.write_text(json.dumps(existing_reviews()))
            original = (source.read_bytes(), existing.read_bytes())
            for dry_run in (True, False):
                extra = ["--existing-reviews", str(existing), *(["--dry-run"] if dry_run else [])]
                summary = json.loads(self.invoke(source, out, extra=extra).stdout)
                self.assertEqual(summary, {"dryRun": dry_run,
                    "fingerprint": nested()["findings"][0]["fingerprint"], "maxOccurrences": 3,
                    "existingEntries": 3, "addedEntries": 1, "totalEntries": 4})
                self.assertEqual(out.exists(), not dry_run)
                self.assertEqual((source.read_bytes(), existing.read_bytes()), original)
            expected = bridge.draft_exception(delta(), nested()["findings"][0]["fingerprint"],
                                              **REVIEW, existing_reviews=existing_reviews())
            self.assertEqual(json.loads(out.read_text()), expected)

    def test_cli_existing_reviews_errors_write_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            source, existing, out, alias = [Path(directory) / name for name in
                                           ("delta.json", "existing.json", "out.json", "alias.json")]
            source.write_text(json.dumps(delta()))
            own = bridge.draft_exception(delta(), nested()["findings"][0]["fingerprint"], **REVIEW)
            invalid = ['null', '[]', '{}', '{bad json', '{"a":1,"a":2}', '{"a":NaN}',
                       json.dumps(own), json.dumps({**existing_reviews(), "extra": 1})]
            for raw in invalid:
                existing.write_text(raw)
                for dry_run in ([], ["--dry-run"]):
                    result = self.invoke(source, out, extra=["--existing-reviews", str(existing), *dry_run], expected=2)
                    self.assertNotIn("unrecognized arguments", result.stderr)
                    self.assertEqual(existing.read_text(), raw)
                    self.assertFalse(out.exists())
                    self.assertEqual(set(Path(directory).iterdir()), {source, existing})
            existing.write_text(json.dumps(existing_reviews()))
            original = existing.read_bytes()
            alias.hardlink_to(existing)
            for destination in (existing, alias):
                self.invoke(source, destination, extra=["--existing-reviews", str(existing)], expected=2)
                self.assertEqual(existing.read_bytes(), original)
            out.write_text("retained output")
            for dry_run in ([], ["--dry-run"]):
                self.invoke(source, out, extra=["--existing-reviews", str(existing), *dry_run], expected=2)
                self.assertEqual(out.read_text(), "retained output")
            out.unlink()
            for path in ("", str(Path(directory) / "absent.json")):
                self.invoke(source, out, extra=["--existing-reviews", path], expected=2)
                self.assertFalse(out.exists())

    def invoke(self, source, out, *, extra=(), omit=(), expected=0):
        args = [sys.executable, "-m", "qawrench.a11y", "draft-exception", str(source),
                "--fingerprint", nested()["findings"][0]["fingerprint"], "--out", str(out)]
        for key, value in REVIEW.items():
            if key not in omit:
                args.extend(["--" + key, value])
        result = subprocess.run([*args, *extra], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return result

    def test_cli_draft_can_be_applied_and_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            source, out = [Path(directory) / name for name in ("delta.json", "review.json")]
            source.write_text(json.dumps(delta()))
            summary = json.loads(self.invoke(source, out, extra=["--dry-run"]).stdout)
            self.assertEqual(summary, {"dryRun": True, "fingerprint": nested()["findings"][0]["fingerprint"],
                                       "maxOccurrences": 3})
            self.assertFalse(out.exists())
            self.invoke(source, out)
            policy = json.loads(out.read_text())
            self.assertTrue(bridge.compare(nested(), nested(3), exceptions=policy, fail_on="serious")["gate"]["passed"])

    def test_cli_errors_preserve_input_and_existing_destinations(self):
        with tempfile.TemporaryDirectory() as directory:
            source, out, alias = [Path(directory) / name for name in ("delta.json", "out.json", "alias.json")]
            source.write_text(json.dumps(delta()))
            original = source.read_bytes()
            alias.hardlink_to(source)
            for destination in (source, alias):
                self.invoke(source, destination, expected=2)
                self.assertEqual(source.read_bytes(), original)
            out.write_text("previous reviews must survive")
            for extra in ([], ["--dry-run"]):
                self.invoke(source, out, extra=extra, expected=2)
                self.assertEqual(out.read_text(), "previous reviews must survive")
            out.unlink()
            out.symlink_to(Path(directory) / "absent.json")
            self.invoke(source, out, expected=2)
            self.assertTrue(out.is_symlink())
            out.unlink()
            for field in REVIEW:
                self.invoke(source, out, omit=[field], expected=2)
                self.assertFalse(out.exists())
            for extra in (["--owner", " "], ["--reason", ""], ["--expires", "bad"],
                          ["--fingerprint", "0" * 64]):
                self.invoke(source, out, extra=extra, expected=2)
                self.assertFalse(out.exists())
            for invalid in ('null', '{"kind":"a11y-delta","kind":"a11y-delta"}',
                            '{"counts":NaN}', json.dumps({**delta(), "counts": {}})):
                source.write_text(invalid)
                self.invoke(source, out, expected=2)
                self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
