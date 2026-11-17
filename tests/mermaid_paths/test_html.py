"""Structural HTML conformance of standalone and embedded appendices.

The original used html-validate's recommended rules (npm). This port checks the
equivalent structural rules with the standard library (see ``support.html_problems``).
"""
from qawrench.mermaid_paths import APPENDIX_STYLES, apply_review, parse_flowchart, render_appendix

from .support import EXAMPLES, SAMPLE, html_problems


def test_standalone_and_embedded_appendices_pass_structural_html_checks():
    graph = apply_review(parse_flowchart(SAMPLE), None, draft=True)
    for fragment in (False, True):
        body = render_appendix(graph, fragment=fragment)
        html = (f'<!DOCTYPE html><html lang="en"><head><title>Host document</title><style>{APPENDIX_STYLES}</style>'
                f"</head><body><h1>Host document</h1>{body}</body></html>") if fragment else body
        assert html_problems(html) == []


def test_committed_example_pages_pass_structural_html_checks():
    for name in ("review-process.html", "approval-zh.html"):
        assert html_problems((EXAMPLES / name).read_text(encoding="utf-8")) == [], name


def test_the_checker_reports_real_problems():
    assert html_problems("<p><a href='#missing'>x</a></p>", document=False) == ["unresolved link '#missing'"]
    assert "</div> closes <p>" in html_problems("<p></div>", document=False)
    assert "<p> not permitted in <ul>" in html_problems("<ul><p>x</p></ul>", document=False)
