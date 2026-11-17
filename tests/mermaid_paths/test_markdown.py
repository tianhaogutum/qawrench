"""Mermaid fences in Markdown: nested containers, info strings, selection and review binding."""
import json

from qawrench.mermaid_paths import apply_review, create_review, extract_mermaid_blocks, parse_flowchart, select_mermaid_block

from .support import raises, run


def fence(source, lang="mermaid", delimiter="```"):
    return f"{delimiter}{lang}\n{source}\n{delimiter}\n"


def test_markdown_extraction_uses_real_fence_parsing_including_tildes_nested_containers_and_info_strings():
    doc = ("# Process\n\n" + fence("flowchart TD\n A-->B")
           + "\n- Nested step:\n\n  ~~~Mermaid title=second\n  graph RL\n   X-->Y\n  ~~~\n")
    blocks = extract_mermaid_blocks(doc)
    assert len(blocks) == 2
    assert [b["index"] for b in blocks] == [1, 2]
    assert [n["id"] for n in parse_flowchart(blocks[0]["source"])["nodes"]] == ["A", "B"]
    assert [n["id"] for n in parse_flowchart(blocks[1]["source"])["nodes"]] == ["X", "Y"]


def test_fences_in_block_quotes_are_found_and_indented_code_is_not_a_fence():
    doc = "> quoted\n>\n> ```mermaid\n> flowchart TD\n>  Q-->R\n> ```\n\n    ```mermaid\n    flowchart TD\n    ```\n"
    blocks = extract_mermaid_blocks(doc)
    assert [b["source"] for b in blocks] == ["flowchart TD\n Q-->R\n"]


def test_literal_mermaid_looking_prose_and_non_mermaid_fences_are_not_treated_as_diagrams():
    doc = ("`flowchart TD; A-->B`\n\n" + fence("flowchart TD\n A-->B", "text") + "\n<!--\n"
           + fence("flowchart TD\n Hidden-->Other") + "-->\n")
    assert extract_mermaid_blocks(doc) == []
    raises("NO_DIAGRAM", select_mermaid_block, doc)


def test_multiple_diagrams_require_an_explicit_bounded_one_based_selection():
    doc = fence("flowchart TD\n A-->B") + fence("sequenceDiagram\n A->>B: Message")
    raises("DIAGRAM_SELECTION", select_mermaid_block, doc)
    assert select_mermaid_block(doc, diagram=1)["index"] == 1
    raises("DIAGRAM_TYPE", parse_flowchart, select_mermaid_block(doc, diagram=2)["source"])
    for diagram in (0, -1, 1.5, 3, "1", float("nan"), float("inf"), True):
        raises("DIAGRAM_SELECTION", select_mermaid_block, doc, diagram=diagram)


def test_review_binding_covers_canonical_extracted_diagram_text_rather_than_surrounding_prose():
    original = "# One\r\n\r\n" + fence("flowchart TD\n A-->B").replace("\n", "\r\n")
    selected = select_mermaid_block(original)
    assert selected["source"] == "flowchart TD\n A-->B\n"
    graph = parse_flowchart(selected["source"])
    review = create_review(graph)
    review["edges"][0]["confirmed"] = True
    prose_changed = parse_flowchart(select_mermaid_block(original.replace("# One", "# Different surrounding prose"))["source"])
    assert apply_review(prose_changed, review)["reviewed"] is True
    diagram_changed = parse_flowchart(select_mermaid_block(original.replace("A-->B", "A-->C"))["source"])
    raises("STALE_REVIEW", apply_review, diagram_changed, review)


def test_markdown_labels_and_extraction_share_no_mutable_state():
    # The original guarded against a host application's global Marked plugins; the
    # Python port has no global Markdown state, so repeated use must stay identical.
    source = 'flowchart TD\n A["`**Author wording**`"] --> B'
    for _ in range(2):
        assert parse_flowchart(source)["nodes"][0]["label"] == "Author wording"
        assert extract_mermaid_blocks(fence(source))[0]["source"] == source + "\n"


def test_cli_reads_markdown_stdin_and_requires_deliberate_multi_diagram_selection():
    doc = fence("flowchart TD\n A-->B") + fence("flowchart TD\n X-->Y")
    assert run("inspect", "-", "--markdown", input=doc)[0] == 2
    status, stdout, stderr = run("inspect", "-", "--markdown", "--diagram", "2", input=doc)
    assert status == 0, stderr
    assert [n["id"] for n in json.loads(stdout)["nodes"]] == ["X", "Y"]
    assert run("inspect", "-", "--diagram", "2", input=doc)[0] == 2
    assert run("inspect", "-", "--markdown", "--diagram", "1.5", input=doc)[0] == 2
