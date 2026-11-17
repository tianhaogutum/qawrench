"""Adapt a parsed Mermaid flowchart to the appendix graph. Unsupported semantics fail rather than disappear."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata

from . import mermaid
from ._js import CONTROL, JS_WHITESPACE, utf16_len
from .errors import at, fail
from .text import label_text

LIMITS = {"sourceBytes": 1_048_576, "syntaxCharacters": 50_000, "nodes": 1_000, "edges": 5_000}
GRAPH_SCHEMA = "mermaid-paths/graph-v1"
_SURROGATE = re.compile("[\ud800-\udfff]")
_ASCII_SYNTAX = re.compile(r"[\t\n\x0b\x0c\r !-/:-@\[-`{-~]")
_NON_ASCII = re.compile(r"[^\x00-\x7f]")
_NUMERIC_HTML_ENTITY = re.compile(r"&#(?:[xX][0-9a-fA-F]+|[0-9]+);")
NOTE_DIRECTIVES = "Mermaid configuration directives are not executed or interpreted."
NOTE_FRONTMATTER = "Frontmatter configuration is not interpreted; use accTitle and accDescr for appendix text."
NOTE_STYLES = "Styles and classes are not interpreted; express meaningful distinctions in text."
NOTE_DIRECTION = "Direction controls layout only and is not interpreted as process order."
STYLE_STATEMENTS = frozenset(("FlowchartClassDefStatement", "FlowchartClassStatement", "FlowchartStyleStatement",
                              "FlowchartLinkStyleStatement", "FlowchartDefaultLinkStyleStatement"))


def _utf8(source: str) -> bytes:
    # JavaScript encodes a lone surrogate as U+FFFD.
    return _SURROGATE.sub("\ufffd", source).encode("utf-8")


def source_hash(source: str) -> str:
    return hashlib.sha256(_utf8(source)).hexdigest()


def assert_source_budget(source) -> None:
    if not isinstance(source, str):
        fail("INPUT", "Mermaid source must be a string.")
    if len(_utf8(source)) > LIMITS["sourceBytes"]:
        fail("LIMIT", "Source exceeds 1 MiB.")
    if CONTROL.search(source):
        fail("INPUT", "Source contains unsupported control characters.")
    # This is a conservative resource bound, not a second Mermaid grammar: a small
    # file can contain tens of thousands of repeated declarations.
    syntax = len(_ASCII_SYNTAX.findall(source))
    counted: dict[str, bool] = {}
    for ch in _NON_ASCII.findall(source):
        hit = counted.get(ch)
        if hit is None:
            hit = counted[ch] = ch in JS_WHITESPACE or unicodedata.category(ch)[0] in "PS"
        syntax += hit
    if syntax > LIMITS["syntaxCharacters"]:
        fail("LIMIT", "Source exceeds the 50,000 whitespace/punctuation/symbol complexity budget; "
             "split the diagram.")


def _strongly_connected(count: int, successors: list[list[int]]) -> list[list[int]]:
    """Tarjan's strongly connected components, iteratively."""
    index_of = [-1] * count
    low = [0] * count
    on_stack = [False] * count
    stack: list[int] = []
    components: list[list[int]] = []
    counter = 0
    for root in range(count):
        if index_of[root] != -1:
            continue
        work = [(root, 0)]
        index_of[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack[root] = True
        while work:
            node, child = work[-1]
            if child < len(successors[node]):
                work[-1] = (node, child + 1)
                following = successors[node][child]
                if index_of[following] == -1:
                    index_of[following] = low[following] = counter
                    counter += 1
                    stack.append(following)
                    on_stack[following] = True
                    work.append((following, 0))
                elif on_stack[following]:
                    low[node] = min(low[node], index_of[following])
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index_of[node]:
                component = []
                while True:
                    member = stack.pop()
                    on_stack[member] = False
                    component.append(member)
                    if member == node:
                        break
                components.append(component)
    return components


class _PriorityQueue:
    """graphlib 4.0.7's binary heap, including its tie-breaking, so shortest paths match."""

    def __init__(self):
        self.arr: list[list] = []
        self.index: dict[int, int] = {}

    def add(self, key: int, priority: float) -> None:
        if key not in self.index:
            self.index[key] = len(self.arr)
            self.arr.append([key, priority])
            self._decrease(len(self.arr) - 1)

    def remove_min(self) -> int:
        self._swap(0, len(self.arr) - 1)
        key = self.arr.pop()[0]
        del self.index[key]
        self._heapify(0)
        return key

    def decrease(self, key: int, priority: float) -> None:
        position = self.index[key]
        self.arr[position][1] = priority
        self._decrease(position)

    def _heapify(self, position: int) -> None:
        arr = self.arr
        while True:
            left = 2 * position
            right = left + 1
            smallest = position
            if left >= len(arr):
                return
            if arr[left][1] < arr[smallest][1]:
                smallest = left
            if right < len(arr) and arr[right][1] < arr[smallest][1]:
                smallest = right
            if smallest == position:
                return
            self._swap(position, smallest)
            position = smallest

    def _decrease(self, position: int) -> None:
        arr = self.arr
        priority = arr[position][1]
        while position != 0:
            parent = position >> 1
            if arr[parent][1] < priority:
                break
            self._swap(position, parent)
            position = parent

    def _swap(self, a: int, b: int) -> None:
        arr = self.arr
        arr[a], arr[b] = arr[b], arr[a]
        self.index[arr[a][0]] = a
        self.index[arr[b][0]] = b


def _dijkstra(count: int, out_targets: list[list[int]], source: int) -> list[int | None]:
    """Predecessors on unit-weight shortest paths from ``source`` (graphlib's algorithm)."""
    distance = [0.0 if v == source else float("inf") for v in range(count)]
    predecessor: list[int | None] = [None] * count
    queue = _PriorityQueue()
    for v in range(count):
        queue.add(v, distance[v])
    while queue.arr:
        v = queue.remove_min()
        if distance[v] == float("inf"):
            break
        for w in out_targets[v]:
            candidate = distance[v] + 1
            if candidate < distance[w]:
                distance[w] = candidate
                predecessor[w] = v
                queue.decrease(w, candidate)
    return predecessor


def parse_flowchart(source: str) -> dict:
    """Parse Mermaid flowchart source into the ``mermaid-paths/graph-v1`` structure."""
    assert_source_budget(source)
    try:
        diagram = mermaid.parse(source)
    except mermaid.ParseError as error:
        fail("PARSE", f"Mermaid parse error{at(error.loc)}: {error.message}")
    except RecursionError:
        fail("PARSE", "Mermaid parse error: the source nests too deeply.")
    if diagram.kind != "flowchart":
        fail("DIAGRAM_TYPE", "Only flowchart or graph diagrams are supported.")
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    warnings: dict[str, None] = {}
    title = None
    description = None
    if diagram.has_directives:
        warnings[NOTE_DIRECTIVES] = None
    if diagram.has_frontmatter:
        warnings[NOTE_FRONTMATTER] = None

    def where(node) -> str:
        return at(diagram.location(node))

    def text(label):
        if label is not None and _NUMERIC_HTML_ENTITY.search(label["value"]):
            fail("ENTITY_AMBIGUITY", "HTML numeric entities can render differently in Mermaid; use Mermaid decimal "
                 "entities such as #9829; instead of &#9829; or &#x2665;.")
        return label_text(None if label is None else {"value": label["value"], "kind": label.get("kind")})

    def vertex(item) -> str:
        if item["data"]:
            fail("UNSUPPORTED", f"Node data blocks (@{{...}}) are not supported{where(item)}; use a basic text label.")
        node_id = item["id"]
        if utf16_len(node_id) > 20_000:
            fail("LIMIT", f"Node identifier exceeds 20,000 characters{where(item)}.")
        if node_id not in nodes:
            if len(nodes) >= LIMITS["nodes"]:
                fail("LIMIT", f"Diagram exceeds {LIMITS['nodes']} nodes.")
            nodes[node_id] = {"id": node_id, "label": node_id, "shape": None, "labelSource": "id",
                              "line": diagram.location(item)[0]}
        shape = item["shape"]
        if shape is not None:
            label = text(shape["label"])
            if not label:
                fail("EMPTY_LABEL", f"Node {json.dumps(node_id, ensure_ascii=False)} has no usable text{where(item)}.")
            nodes[node_id].update(label=label, shape=shape["name"], labelSource="authored")
        if item["cssClass"] is not None:
            warnings[NOTE_STYLES] = None
        return node_id

    for statement in diagram.body["body"]:
        kind = statement.type
        if kind == "FlowchartVertexStatement":
            sources = [vertex(v) for v in statement["first"]["vertices"]]
            for link in statement["links"]:
                start_marker = (link["opening"] or link["arrow"])["startMarker"]
                arrow = link["arrow"]
                if start_marker != "none" or arrow["endMarker"] != "arrow" or arrow["line"] == "invisible":
                    fail("UNSUPPORTED_EDGE", f"Only one-way arrow connections are supported{where(link)}; replace "
                         "bidirectional, unheaded, circle, cross, or invisible links.")
                targets = [vertex(v) for v in link["target"]["vertices"]]
                if len(edges) + len(sources) * len(targets) > LIMITS["edges"]:
                    fail("LIMIT", f"Diagram exceeds {LIMITS['edges']} directed connections after fan-out expansion.")
                label = text(link["label"])
                line = diagram.location(link)[0]
                for origin in sources:
                    for target in targets:
                        edges.append({"id": f"e{len(edges) + 1}", "from": origin, "to": target,
                                      "label": label or None, "style": arrow["line"], "authoredId": link["id"],
                                      "line": line})
                sources = targets
        elif kind == "FlowchartAccTitleStatement":
            title = text(statement["value"])
        elif kind == "FlowchartAccDescrStatement":
            description = text(statement["value"])
        elif kind == "FlowchartAccDescrBlockStatement":
            if not statement["closed"]:
                fail("UNTERMINATED_METADATA", f"The accDescr block must close with }}{where(statement)}.")
            description = text(statement["value"])
        elif kind == "FlowchartEmptyStatement":
            pass
        elif kind == "FlowchartDirectionStatement":
            warnings[NOTE_DIRECTION] = None
        elif kind in STYLE_STATEMENTS:
            warnings[NOTE_STYLES] = None
        else:
            fail("UNSUPPORTED", f"{kind[len('Flowchart'):]} is outside the nodes-and-directed-edges subset"
                 f"{where(statement)}.")
    if not nodes:
        fail("EMPTY_GRAPH", "The flowchart contains no nodes.")
    return _analyse(source, title, description, diagram.body["direction"], list(warnings), nodes, edges)


def _analyse(source, title, description, direction, warnings, nodes, edges) -> dict:
    ordered = list(nodes.values())
    order = {node["id"]: i for i, node in enumerate(ordered)}
    count = len(ordered)
    out_targets: list[list[int]] = [[] for _ in range(count)]
    for edge in edges:
        out_targets[order[edge["from"]]].append(order[edge["to"]])
    components = sorted((sorted(group) for group in _strongly_connected(count, out_targets)), key=lambda g: g[0])
    component_of = {}
    for i, group in enumerate(components):
        for member in group:
            component_of[member] = i
    incoming = {node["id"]: [] for node in ordered}
    outgoing = {node["id"]: [] for node in ordered}
    external_incoming = set()
    self_loops = set()
    for edge in edges:
        incoming[edge["to"]].append(edge["id"])
        outgoing[edge["from"]].append(edge["id"])
        a, b = order[edge["from"]], order[edge["to"]]
        if component_of[a] != component_of[b]:
            external_incoming.add(component_of[b])
        if a == b:
            self_loops.add(a)
    loops = []
    loop_of: dict[int, dict] = {}
    for group in components:
        if len(group) > 1 or group[0] in self_loops:
            loop = {"id": f"loop-{len(loops) + 1}", "nodes": [ordered[m]["id"] for m in group], "edges": []}
            loops.append(loop)
            for member in group:
                loop_of[member] = loop
    first_in_loop: dict[str, dict] = {}
    for edge in edges:
        loop = loop_of.get(order[edge["from"]])
        edge["loop"] = loop["id"] if loop is not None and loop is loop_of.get(order[edge["to"]]) else None
        if edge["loop"]:
            loop["edges"].append(edge["id"])
            if edge["from"] == loop["nodes"][0] and loop["id"] not in first_in_loop:
                first_in_loop[loop["id"]] = edge
    edge_by_pair: dict[tuple[int, int], str] = {}
    for edge in edges:
        edge_by_pair.setdefault((order[edge["from"]], order[edge["to"]]), edge["id"])
    for loop in loops:
        first = first_in_loop[loop["id"]]
        if first["from"] == first["to"]:
            loop["witness"] = [first["id"]]
            continue
        start, goal = order[first["to"]], order[first["from"]]
        predecessor = _dijkstra(count, out_targets, start)
        reverse = []
        cursor = goal
        while cursor != start:
            previous = predecessor[cursor]
            reverse.append(edge_by_pair[(previous, cursor)])
            cursor = previous
        loop["witness"] = [first["id"], *reversed(reverse)]
    entries = [{"node": ordered[group[0]]["id"],
                "kind": "loop-entry-candidate" if group[0] in loop_of else "no-incoming-connections"}
               for i, group in enumerate(components) if i not in external_incoming]
    return {
        "schema": GRAPH_SCHEMA, "sourceSha256": source_hash(source),
        "title": title, "description": description, "direction": direction, "warnings": warnings,
        "nodes": [{**node, "incoming": incoming[node["id"]], "outgoing": outgoing[node["id"]],
                   "loop": loop_of[i]["id"] if i in loop_of else None} for i, node in enumerate(ordered)],
        "edges": edges, "loops": loops, "entries": entries,
    }
