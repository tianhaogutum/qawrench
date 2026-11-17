"""CLI behaviour: review/build workflow, safe output, stdin/stdout, encodings and exit codes."""
import hashlib
import json
import os

from qawrench import __version__
from qawrench.mermaid_paths.appendix import APPENDIX_STYLES

from .support import EXAMPLES, run


def test_end_to_end_review_author_edit_strict_build_and_source_preservation(tmp_path):
    source, review, html = tmp_path / "source.mmd", tmp_path / "review.json", tmp_path / "appendix.html"
    text = "flowchart TD\n A[Draft] -->|Ready| B[Review]\n B --> A"
    source.write_text(text, encoding="utf-8")
    assert run("review", source, "-o", review)[0] == 0
    data = json.loads(review.read_text(encoding="utf-8"))
    for edge in data["edges"]:
        edge["confirmed"] = True
        if not edge["sourceLabel"]:
            edge["text"] = "Revise the draft"
    review.write_text(json.dumps(data), encoding="utf-8")
    status, _, stderr = run("build", source, "--review", review, "-o", html)
    assert status == 0, stderr
    assert "Revise the draft" in html.read_text(encoding="utf-8")
    assert source.read_text(encoding="utf-8") == text
    status, _, stderr = run("build", source, "--review", review, "-o", html)
    assert status == 2
    assert "OUTPUT_EXISTS" in stderr
    assert run("build", source, "--review", review, "-o", html, "--force")[0] == 0
    assert sorted(os.listdir(tmp_path)) == ["appendix.html", "review.json", "source.mmd"]


def test_refuses_source_and_review_overwrites_including_output_symlinks(tmp_path):
    source, alias = tmp_path / "source.mmd", tmp_path / "alias.html"
    source.write_text("flowchart TD\n A --> B", encoding="utf-8")
    alias.symlink_to(source)
    for output in (source, alias):
        status, _, stderr = run("build", source, "--draft", "-o", output, "--force")
        assert status == 2, stderr
        assert "OUTPUT_INPUT" in stderr
    assert source.read_text(encoding="utf-8") == "flowchart TD\n A --> B"


def test_stdin_stdout_and_utf8_corruption_fails_without_partial_output():
    status, stdout, stderr = run("build", "-", "--draft", input="graph LR; A[你好] --> B")
    assert status == 0, stderr
    assert "你好" in stdout
    status, stdout, stderr = run("inspect", "-", input=bytes([0xFF, 0xFE, 0x00]))
    assert status == 2
    assert stdout == ""
    assert "ENCODING" in stderr


def test_malformed_reviews_are_explained_and_create_no_output(tmp_path):
    source, review, output = tmp_path / "source.mmd", tmp_path / "broken.json", tmp_path / "out.html"
    source.write_text("flowchart TD; A-->B", encoding="utf-8")
    review.write_text("{broken", encoding="utf-8")
    status, _, stderr = run("build", source, "--review", review, "-o", output)
    assert status == 2
    assert "REVIEW_JSON" in stderr
    assert sorted(os.listdir(tmp_path)) == ["broken.json", "source.mmd"]


def test_review_hash_includes_a_utf8_bom_and_source_bom_changes_invalidate_it(tmp_path):
    source, review = tmp_path / "source.mmd", tmp_path / "review.json"
    data = "﻿flowchart TD\n A-->B".encode("utf-8")
    source.write_bytes(data)
    status, stdout, stderr = run("review", source)
    assert status == 0, stderr
    template = json.loads(stdout)
    assert template["sourceSha256"] == hashlib.sha256(data).hexdigest()
    for edge in template["edges"]:
        edge["confirmed"] = True
    review.write_text("﻿" + json.dumps(template), encoding="utf-8")
    assert run("build", source, "--review", review)[0] == 0
    source.write_text("flowchart TD\n A-->B", encoding="utf-8")
    status, _, stderr = run("build", source, "--review", review)
    assert status == 2
    assert "STALE_REVIEW" in stderr


def test_unicode_spaced_paths_work_and_failed_forced_builds_preserve_existing_output(tmp_path):
    source, review, output = tmp_path / "流程 example.mmd", tmp_path / "review data.json", tmp_path / "appendix 文.html"
    source.write_text("flowchart TD\n A[开始] --> B[结束]", encoding="utf-8")
    status, stdout, stderr = run("review", source)
    assert status == 0, stderr
    data = json.loads(stdout)
    data["edges"][0]["confirmed"] = True
    review.write_text(json.dumps(data), encoding="utf-8")
    assert run("build", source, "--review", review, "-o", output)[0] == 0
    original = output.read_text(encoding="utf-8")
    source.write_text("flowchart TD\n A[开始] --> C[已修改]", encoding="utf-8")
    status, _, stderr = run("build", source, "--review", review, "-o", output, "--force")
    assert status == 2
    assert "STALE_REVIEW" in stderr
    assert output.read_text(encoding="utf-8") == original


def test_json_export_expansion_is_bounded_before_writing_any_output(tmp_path):
    source, output = tmp_path / "expanding.mmd", tmp_path / "existing.json"
    targets = " & ".join(f"t{i}" for i in range(999))
    source.write_text(f"flowchart TD\n {'n' * 20_000} --> {targets}", encoding="utf-8")
    output.write_text("existing output", encoding="utf-8")
    for command in ("review", "inspect"):
        status, stdout, stderr = run(command, source, "-o", output, "--force")
        assert status == 2, stderr
        assert stdout == ""
        assert "OUTPUT_LIMIT" in stderr
        assert output.read_text(encoding="utf-8") == "existing output"


def test_explicit_null_review_or_empty_review_path_cannot_silently_become_no_review(tmp_path):
    source, review = tmp_path / "source.mmd", tmp_path / "null.json"
    source.write_text("flowchart TD\n A-->B", encoding="utf-8")
    review.write_text("null", encoding="utf-8")
    status, stdout, stderr = run("build", source, "--draft", "--review", review)
    assert status == 2
    assert stdout == ""
    assert "REVIEW_SCHEMA" in stderr
    status, _, stderr = run("build", source, "--draft", "--review", "")
    assert status == 2
    assert "USAGE" in stderr
    assert run("inspect", source, "-o", "")[0] == 2


def test_errors_use_the_new_program_name_and_exit_codes(tmp_path):
    status, _, stderr = run("inspect", tmp_path / "missing.mmd")
    assert status == 1
    assert stderr.startswith("qawrench mermaid-paths: ENOENT: ")
    status, _, stderr = run("build", "-", "--draft", "--review", "-", input="flowchart TD\n A-->B")
    assert (status, stderr) == (2, "qawrench mermaid-paths: USAGE: The review JSON must be a file; stdin is "
                                   "reserved for Mermaid source.\n")
    assert run("review", "-", "--title", "x", input="flowchart TD\n A-->B")[0] == 2
    assert run("inspect", "-", "--lang", "en", input="flowchart TD\n A-->B")[0] == 2
    assert run("styles", "--draft")[0] == 2


def test_version_help_and_styles():
    assert run("--version")[:2] == (0, f"{__version__}\n")
    status, stdout, _ = run("--help")
    assert status == 0
    assert stdout.startswith("usage: qawrench mermaid-paths")
    for command in ("review", "inspect", "build", "styles"):
        assert f"qawrench mermaid-paths {command}" in stdout
    status, stdout, _ = run("styles")
    assert status == 0
    assert stdout == APPENDIX_STYLES.lstrip()


def test_committed_examples_regenerate_byte_identically(tmp_path):
    cases = [
        ("review-process.mmd", "review-process.review.json", ["--label-lang", "en"], "review-process.html"),
        ("approval-zh.mmd", "approval-zh.review.json", ["--lang", "zh-CN", "--label-lang", "zh-CN"], "approval-zh.html"),
    ]
    for source, review, options, expected in cases:
        output = tmp_path / expected
        status, _, stderr = run("build", EXAMPLES / source, "--review", EXAMPLES / review, *options, "-o", output)
        assert status == 0, stderr
        assert output.read_bytes() == (EXAMPLES / expected).read_bytes()
    status, stdout, stderr = run("build", EXAMPLES / "workflow-doc.md", "--markdown",
                                 "--review", EXAMPLES / "review-process.review.json", "--label-lang", "en")
    assert status == 0, stderr
    assert stdout == (EXAMPLES / "review-process.html").read_text(encoding="utf-8")


def test_review_template_matches_the_committed_example_context():
    status, stdout, stderr = run("review", EXAMPLES / "approval-zh.mmd", "--lang", "zh-CN")
    assert status == 0, stderr
    generated = json.loads(stdout)
    committed = json.loads((EXAMPLES / "approval-zh.review.json").read_text(encoding="utf-8"))
    assert list(generated) == list(committed)
    assert {k: generated[k] for k in ("schema", "sourceSha256", "instructions")} == \
        {k: committed[k] for k in ("schema", "sourceSha256", "instructions")}
    for made, kept in zip(generated["edges"], committed["edges"]):
        assert list(made) == list(kept)
        assert {k: v for k, v in made.items() if k not in ("text", "confirmed")} == \
            {k: v for k, v in kept.items() if k not in ("text", "confirmed")}
        assert made["confirmed"] is False
