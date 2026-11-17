"""Independent reachability/entry checks over deterministic mixed-ID graphs, and review context fields."""
from qawrench.mermaid_paths import apply_review, create_review, parse_flowchart, render_appendix

IDS = ["__proto__", "constructor", "toString", "节点", "a-b", "x.y", "A", "B", "C", "D"]
ARROWS = {"solid": "-->", "dotted": "-.->", "thick": "==>"}


def test_100_deterministic_mixed_id_graphs_agree_with_independent_reachability_and_entry_analysis():
    seed = 0x1794

    def random(n):
        nonlocal seed
        seed = (seed * 1664525 + 1013904223) & 0xFFFFFFFF
        return seed % n

    for round_number in range(100):
        truth = []
        for i in range(random(31)):
            truth.append({"from": IDS[random(len(IDS))], "to": IDS[random(len(IDS))], "label": f"edge{i}",
                          "style": ["solid", "dotted", "thick"][random(3)]})
        direction = ["LR", "RL", "TD", "BT"][round_number % 4]
        source = (f"flowchart {direction}\n" + "\n".join(f'{i}["Label {i}"]' for i in IDS) + "\n"
                  + "\n".join(f"{e['from']} {ARROWS[e['style']]}|{e['label']}| {e['to']}" for e in truth))
        graph = parse_flowchart(source)
        assert [{k: e[k] for k in ("from", "to", "label", "style")} for e in graph["edges"]] == truth
        outgoing = {i: [e["to"] for e in truth if e["from"] == i] for i in IDS}

        def reachable(start, end):
            seen, pending = set(), [start]
            while pending:
                node = pending.pop()
                if node == end:
                    return True
                if node in seen:
                    continue
                seen.add(node)
                pending.extend(outgoing[node])
            return False

        for edge in graph["edges"]:
            assert bool(edge["loop"]) == reachable(edge["to"], edge["from"]), f"round {round_number} {edge['id']}"
        unassigned, expected_entries = set(IDS), []
        for node in IDS:
            if node not in unassigned:
                continue
            group = [other for other in IDS if reachable(node, other) and reachable(other, node)]
            unassigned.difference_update(group)
            members = set(group)
            external = any(e["to"] in members and e["from"] not in members for e in truth)
            if not external:
                loop = len(group) > 1 or any(e["from"] == node and e["to"] == node for e in truth)
                expected_entries.append({"node": node, "kind": "loop-entry-candidate" if loop
                                         else "no-incoming-connections"})
        assert graph["entries"] == expected_entries, f"round {round_number}: entries"
        for loop in graph["loops"]:
            route = [next(e for e in graph["edges"] if e["id"] == i) for i in loop["witness"]]
            assert 0 < len(route) <= len(loop["nodes"])
            assert route[-1]["to"] == route[0]["from"]
            for i in range(1, len(route)):
                assert route[i - 1]["to"] == route[i]["from"]
            assert all(e["loop"] == loop["id"] for e in route)


def test_review_context_exposes_real_source_lines_endpoint_labels_and_line_styles_with_legacy_compatibility():
    graph = parse_flowchart("flowchart RL\n A[Start] -. optional .-> B[Finish]")
    review = create_review(graph)
    first = review["edges"][0]
    assert {k: first[k] for k in ("fromLabel", "toLabel", "sourceLine", "lineStyle")} == \
        {"fromLabel": "Start", "toLabel": "Finish", "sourceLine": 2, "lineStyle": "dotted"}
    html = render_appendix(apply_review(graph, None, draft=True))
    assert '<th scope="col">Line style</th>' in html
    assert "<td>dotted</td>" in html
    for field in ("fromLabel", "toLabel", "sourceLine", "lineStyle"):
        del first[field]
    first["confirmed"] = True
    assert apply_review(graph, review)["reviewed"] is True
