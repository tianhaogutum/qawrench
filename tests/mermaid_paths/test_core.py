"""Graph analysis, review binding, label text and HTML safety (ported from the original core tests)."""
import copy

from qawrench.mermaid_paths import apply_review, create_review, parse_flowchart, render_appendix

from .support import SAMPLE, confirmed, raises, tree


def test_review_revise_workflow_retains_labeled_choices_and_identifies_its_real_loop():
    graph = parse_flowchart(SAMPLE)
    assert graph["title"] == "Review and publish an article"
    assert [[e["from"], e["to"], e["label"]] for e in graph["edges"]] == [
        ["draft", "review", None], ["review", "publish", "Yes, approved"],
        ["review", "revise", "No, revise"], ["revise", "review", None],
    ]
    assert graph["loops"] == [{"id": "loop-1", "nodes": ["review", "revise"], "edges": ["e3", "e4"],
                               "witness": ["e3", "e4"]}]
    assert graph["entries"] == [{"node": "draft", "kind": "no-incoming-connections"}]


def test_chained_fan_out_expands_all_directed_connections_and_preserves_parallel_edges():
    graph = parse_flowchart("graph LR; A & B -->|ready| C & D --> E; A --> C")
    assert [[e["from"], e["to"]] for e in graph["edges"]] == [
        ["A", "C"], ["A", "D"], ["B", "C"], ["B", "D"], ["C", "E"], ["D", "E"], ["A", "C"],
    ]
    assert graph["edges"][0]["label"] == "ready"
    assert len(next(n for n in graph["nodes"] if n["id"] == "C")["incoming"]) == 3


def test_self_loops_disconnected_cycles_and_isolated_nodes_all_have_entry_candidates():
    graph = parse_flowchart("flowchart TD\n A --> A\n B --> C\n C --> B\n D\n E --> F")
    assert [e["node"] for e in graph["entries"]] == ["A", "B", "D", "E"]
    assert len(graph["loops"]) == 2
    html = render_appendix(confirmed(graph))
    assert "Returns directly to this node" in html
    assert "a path returns to" in html


def test_last_explicit_node_label_wins_and_references_do_not_erase_it():
    graph = parse_flowchart("flowchart TD\n A[Old] --> B\n A[New]\n B --> A")
    assert graph["nodes"][0]["label"] == "New"
    assert graph["nodes"][1]["label"] == "B"
    assert graph["nodes"][1]["labelSource"] == "id"


def test_html_markdown_line_breaks_and_mermaid_html_entities_retain_readable_text():
    source = ('flowchart TD\n A["Tom &amp; Jerry<br/>Line <b>two</b>"] --> B["`**Bold** and _italic_`"]\n'
              ' C["Quote #quot; heart #9829; &quot;"]')
    graph = parse_flowchart(source)
    assert [n["label"] for n in graph["nodes"]] == ["Tom & Jerry Line two", "Bold and italic", 'Quote " heart ♥ "']
    for entity in ("&#9829;", "&#x2665;"):
        raises("ENTITY_AMBIGUITY", parse_flowchart, f'flowchart TD\n A["{entity}"]')


def test_unsafe_label_markup_cannot_execute_in_the_static_appendix():
    graph = parse_flowchart('flowchart TD\n A["<script>alert(1)</script><b>Safe</b>"] --> '
                            'B["&lt;script&gt;visible&lt;/script&gt;"]')
    review = create_review(graph)
    review["edges"][0]["text"] = '<img src=x onerror=alert(1)> & "text"'
    review["edges"][0]["confirmed"] = True
    html = render_appendix(apply_review(graph, review))
    assert [e for e in tree(html) if e.tag in ("script", "img", "iframe")] == []
    assert "&lt;img src=x onerror=alert(1)&gt;" in html
    assert graph["nodes"][0]["label"] == "Safe"


def test_all_generated_links_resolve_uniquely_to_focusable_targets():
    for fragment in (False, True):
        html = render_appendix(confirmed(parse_flowchart(SAMPLE)), fragment=fragment, id_prefix="test-chart")
        elements = tree(html)
        ids = [e.attrs["id"] for e in elements if e.attrs.get("id")]
        assert len(set(ids)) == len(ids)
        for anchor in (e for e in elements if e.tag == "a"):
            target = next((e for e in elements if e.attrs.get("id") == anchor.attrs["href"][1:]), None)
            assert target is not None, f"Missing target for {anchor.attrs['href']}"
            assert target.attrs.get("tabindex") == "-1"
        assert len([e for e in elements if e.tag == "th" and e.attrs.get("scope") == "col"]) == 5
        assert len([e for e in elements if e.tag == "section" and e.attrs.get("aria-labelledby")]) == 1
        assert [e for e in elements if e.tag == "script"] == []


def test_strict_publication_requires_every_edge_including_unlabeled_connections_to_be_confirmed():
    graph = parse_flowchart(SAMPLE)
    review = create_review(graph)
    assert review["edges"][0]["text"] == "Unlabeled connection"
    raises("UNCONFIRMED", apply_review, graph)
    raises("UNCONFIRMED", apply_review, graph, review)
    review["edges"][0]["confirmed"] = True
    raises("UNCONFIRMED", apply_review, graph, review)
    draft = apply_review(graph, review, draft=True)
    assert len(draft["unconfirmed"]) == 3
    assert "Draft: 3 connection" in render_appendix(draft)
    assert confirmed(graph)["reviewed"] is True


def test_review_rejects_stale_input_repeated_ids_missing_records_mismatched_endpoints_and_nonboolean_confirmation():
    graph = parse_flowchart(SAMPLE)
    review = create_review(graph)

    def set_edge(index, key, value):
        return lambda r: r["edges"][index].__setitem__(key, value)

    changes = [
        (lambda r: r.__setitem__("sourceSha256", "other"), "STALE_REVIEW"),
        (lambda r: r["edges"].pop(), "REVIEW_EDGES"),
        (lambda r: r["edges"].__setitem__(1, r["edges"][0]), "REVIEW_EDGES"),
        (set_edge(0, "from", "publish"), "REVIEW_EDGES"),
        (set_edge(0, "sourceLabel", "invented"), "REVIEW_EDGES"),
        (set_edge(0, "fromLabel", "Misleading name"), "REVIEW_EDGES"),
        (set_edge(0, "sourceLine", 999), "REVIEW_EDGES"),
        (set_edge(0, "lineStyle", "dotted"), "REVIEW_EDGES"),
        (set_edge(0, "confirmed", "true"), "REVIEW_SCHEMA"),
        (set_edge(0, "text", " "), "INVALID_TEXT"),
    ]
    for change, expected in changes:
        copied = copy.deepcopy(review)
        change(copied)
        raises(expected, apply_review, graph, copied, draft=True)
    raises("STALE_REVIEW", apply_review, parse_flowchart(SAMPLE + "\n"), review, draft=True)


def test_unsupported_relationships_grouping_clicks_and_data_fail_explicitly():
    for arrow in ("---", "<-->", "--o", "--x", "~~~", "o--o"):
        raises("UNSUPPORTED_EDGE", parse_flowchart, f"flowchart TD\n A {arrow} B")
    for source in ("subgraph x\n A-->B\n end", "A@{shape: rect}", 'A-->B\n click A "https://example.com"'):
        raises("UNSUPPORTED", parse_flowchart, f"flowchart TD\n {source}")
    raises("DIAGRAM_TYPE", parse_flowchart, "sequenceDiagram\n A->>B: hi")
    raises("PARSE", parse_flowchart, "flowchart TD\n A[unfinished")
    raises("EMPTY_GRAPH", parse_flowchart, "flowchart TD")


def test_exact_diagnostics_name_the_construct_and_its_position():
    assert str(raises("UNSUPPORTED", parse_flowchart, "flowchart TD\n subgraph x\n A-->B\n end")) == \
        "SubgraphStatement is outside the nodes-and-directed-edges subset at 2:2."
    assert str(raises("UNSUPPORTED_EDGE", parse_flowchart, "flowchart TD\n A --- B")) == (
        "Only one-way arrow connections are supported at 2:4; replace bidirectional, unheaded, circle, cross, "
        "or invisible links.")
    assert str(raises("PARSE", parse_flowchart, "flowchart TD\n A[unfinished")) == \
        "Mermaid parse error at 2:14: Unexpected end of text"
    assert str(raises("UNSUPPORTED", parse_flowchart, "flowchart TD\n A@{shape: rect}")) == \
        "Node data blocks (@{...}) are not supported at 2:2; use a basic text label."


def test_prototype_like_ids_remain_ordinary_nodes_and_never_become_html_ids():
    graph = parse_flowchart("flowchart TD\n __proto__[Prototype] --> constructor[Construct]")
    assert [n["id"] for n in graph["nodes"]] == ["__proto__", "constructor"]
    assert "Prototype" in render_appendix(confirmed(graph))


def test_rejects_oversized_input_graphs_unsafe_ids_and_invalid_language_options():
    raises("LIMIT", parse_flowchart, " " * 1_048_577)
    raises("LIMIT", parse_flowchart, "flowchart TD\n " + "a" * 20_001)
    raises("LIMIT", parse_flowchart, "flowchart TD\n" + "A;\n" * 30_000)
    raises("LIMIT", parse_flowchart, "flowchart TD\n" + "\n".join(f"n{i}" for i in range(1001)))
    graph = confirmed(parse_flowchart("flowchart TD\n A"))
    raises("ID_PREFIX", render_appendix, graph, id_prefix='bad" onclick="x')
    raises("LANGUAGE", render_appendix, graph, lang='en"')
    raises("REVIEW_REQUIRED", render_appendix, parse_flowchart(SAMPLE))


def test_every_loop_has_an_explicit_connected_closed_return_route():
    graph = parse_flowchart("flowchart TD\n A-->B\n B-->C\n C-->A\n B-->A\n C-->D\n D-->E\n E-->D\n F-->F")
    for loop in graph["loops"]:
        route = [next(e for e in graph["edges"] if e["id"] == i) for i in loop["witness"]]
        assert route
        assert route[-1]["to"] == route[0]["from"]
        for i in range(1, len(route)):
            assert route[i]["from"] == route[i - 1]["to"]
        for edge in route:
            assert edge["loop"] == loop["id"]
    assert [loop["witness"] for loop in graph["loops"]] == [["e1", "e4"], ["e6", "e7"], ["e8"]]


def test_decoded_control_characters_and_excessive_repeated_text_fail_before_publication():
    raises("INVALID_TEXT", parse_flowchart, 'flowchart TD\n A["#27;"]')
    targets = " & ".join(f"n{i}" for i in range(999))
    graph = parse_flowchart(f'flowchart TD\n root["{"&amp;" * 3_000}"] --> {targets}')
    raises("OUTPUT_LIMIT", render_appendix, apply_review(graph, None, draft=True))
