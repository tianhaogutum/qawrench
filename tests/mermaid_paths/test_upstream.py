"""The recorded Mermaid 11.12.0 documentation corpus: 44 supported graphs, 66 explicit rejections."""
import hashlib
import json

import pytest

from qawrench.mermaid_paths import AppendixError, apply_review, parse_flowchart, render_appendix

from .support import UPSTREAM

MANIFEST = json.loads((UPSTREAM / "manifest.json").read_text(encoding="utf-8"))
EXPECTED = json.loads((UPSTREAM / "expected.json").read_text(encoding="utf-8"))


def test_44_real_upstream_examples_match_the_official_mermaid_parser_graph():
    assert len(EXPECTED["examples"]) == 44
    for example in EXPECTED["examples"]:
        data = (UPSTREAM / example["file"]).read_bytes()
        metadata = next(e for e in MANIFEST["examples"] if e["file"] == example["file"])
        assert hashlib.sha256(data).hexdigest() == metadata["sha256"], example["file"]
        graph = parse_flowchart(data.decode("utf-8"))
        assert [{"id": n["id"], "label": n["label"]} for n in graph["nodes"]] == example["nodes"], example["file"]
        assert [{"from": e["from"], "to": e["to"], "label": e["label"]} for e in graph["edges"]] == example["edges"], \
            example["file"]
        assert "Semantic node list" in render_appendix(apply_review(graph, None, draft=True)), example["file"]


def test_66_real_upstream_examples_outside_the_subset_fail_with_explicit_unsupported_errors():
    unsupported = [e for e in MANIFEST["examples"] if not e["supported"]]
    assert len(unsupported) == 66
    for example in unsupported:
        with pytest.raises(AppendixError) as info:
            parse_flowchart((UPSTREAM / example["file"]).read_text(encoding="utf-8"))
        assert info.value.code in ("UNSUPPORTED", "UNSUPPORTED_EDGE"), example["file"]


def test_upstream_license_is_kept_next_to_the_fixtures():
    assert (UPSTREAM / "LICENSE.mermaid").read_text(encoding="utf-8").startswith("The MIT License (MIT)")
