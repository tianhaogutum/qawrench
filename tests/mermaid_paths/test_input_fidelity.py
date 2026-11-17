"""Label fidelity: hidden, graphic, structured or unterminated input fails instead of being guessed."""
import pytest

from qawrench.mermaid_paths import apply_review, create_review, parse_flowchart, render_appendix

from .support import raises


@pytest.mark.parametrize("label", [
    "<span hidden>Hidden</span>Visible", '<span style="display:none">Hidden</span>Visible',
    '<span class="hidden">Hidden</span>Visible', '<span aria-hidden="true">Hidden</span>Visible',
    "<svg><text>Graphic</text></svg>", "<math><mi>x</mi></math>",
])
def test_visibility_dependent_markup_and_embedded_graphics_fail(label):
    # Mermaid quotes cannot directly nest attribute quotes; single quotes are
    # equivalent HTML syntax and keep these actual .mmd snippets well formed.
    source = f'flowchart TD\n A["{label.replace(chr(34), chr(39))}"] --> B'
    raises("NON_TEXT_LABEL", parse_flowchart, source)


def test_basic_formatting_is_kept_as_text():
    assert parse_flowchart('flowchart TD\n A["<strong>Visible</strong><br/>text"] --> B')["nodes"][0]["label"] == \
        "Visible text"


def test_image_alt_text_is_preserved_without_fetching_image_resources():
    graph = parse_flowchart("flowchart TD\n A[\"<img src='https://example.invalid/image.svg' alt='Author description'>\"] --> B")
    assert graph["nodes"][0]["label"] == "Author description"
    raises("NON_TEXT_LABEL", parse_flowchart, "flowchart TD\n A[\"<img src='x'>\"] --> B")


def test_structured_labels_fail_instead_of_silently_losing_list_numbering_or_table_associations():
    for value in ("<ol><li>Step one</li><li>Step two</li></ol>",
                  "<table><tr><th>Name</th><td>Value</td></tr></table>"):
        raises("NON_TEXT_LABEL", parse_flowchart, f'flowchart TD\n A["{value}"] --> B')
    raises("NON_TEXT_LABEL", parse_flowchart, 'flowchart TD\n A["`1. Step one\n2. Step two`"] --> B')


def test_unterminated_descriptions_fail_conservatively_rather_than_swallowing_trailing_source():
    raises("UNTERMINATED_METADATA", parse_flowchart, "flowchart TD\n A-->B\n accDescr {\n description\n B-->C")


def test_nonboolean_api_options_cannot_silently_enable_draft_or_fragment_behavior():
    graph = parse_flowchart("flowchart TD\n A-->B")
    for draft in ("false", 1, None):
        raises("OPTIONS", apply_review, graph, None, draft=draft)
    reviewed = apply_review(graph, None, draft=True)
    raises("OPTIONS", render_appendix, reviewed, fragment="false")
    raises("ID_PREFIX", render_appendix, reviewed, id_prefix=None)


def test_partial_review_status_is_announced_in_path_link_names_and_return_route_text():
    graph = parse_flowchart("flowchart TD\n A -->|Pending wording| B\n B -->|Reviewed wording| A")
    review = create_review(graph)
    review["edges"][1]["confirmed"] = True
    html = render_appendix(apply_review(graph, review, draft=True))
    import re
    assert re.search(r'<a href="[^"]+"><bdi>Pending wording</bdi> \(unconfirmed\) — go to', html)
    assert re.search(r"<li>From [^\n]+Pending wording</bdi> \(unconfirmed\)", html)
    assert not re.search(r"Reviewed wording</bdi> \(unconfirmed\)", html)
