"""Static, script-free HTML appendix rendering."""
from __future__ import annotations

import re
from typing import Any

from ._js import JS_WHITESPACE
from .errors import fail
from .i18n import interface_language, language_tag
from .text import escape_html as h, plain_text

APPENDIX_STYLES = """
.mermaid-paths { color: #172033; background: #fff; font: 1rem/1.65 system-ui, sans-serif; max-width: 72rem; margin: auto; padding: 1.5rem; overflow-wrap: anywhere; }
.mermaid-paths a { color: #064c9e; text-decoration: underline; text-underline-offset: .15em; }
.mermaid-paths :focus-visible { outline: 3px solid #6b21a8; outline-offset: 4px; }
.mermaid-paths :target { outline: 3px solid #6b21a8; outline-offset: 4px; scroll-margin-top: 1rem; }
.mermaid-paths th, .mermaid-paths td { border: 1px solid #9aa7b8; padding: .65rem; vertical-align: top; text-align: left; }
.mermaid-paths table { border-collapse: collapse; width: 100%; }
.mermaid-paths caption { text-align: left; font-weight: bold; padding: .5rem 0; }
.mermaid-paths section { margin-block: 2rem; }
.mermaid-paths li { margin-block: .5rem; }
.mermaid-paths .draft { border: 3px solid #9a3412; padding: 1rem; }
.mermaid-paths code { font-size: .95em; }
@media print { .mermaid-paths { max-width: none; padding: 0; } .mermaid-paths :target { outline: none; } }
"""

MAX_OUTPUT_BYTES = 16_777_216
_ID_PREFIX = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}")
_DEFAULT = object()
WARNING_KEYS = {
    "Mermaid configuration directives are not executed or interpreted.": "noteDirectives",
    "Frontmatter configuration is not interpreted; use accTitle and accDescr for appendix text.": "noteFrontmatter",
    "Styles and classes are not interpreted; express meaningful distinctions in text.": "noteStyles",
    "Direction controls layout only and is not interpreted as process order.": "noteDirection",
}
DASH = "\u2014"


def _bytes(value: str) -> int:
    return len(value.encode("utf-8", "surrogatepass"))


def _check_expansion_budget(graph: dict, title: str) -> None:
    names = {node["id"]: _bytes(h(node["label"]) + h(node["id"])) for node in graph["nodes"]}
    # Labels/IDs repeat across the index, adjacency, incoming/outgoing links and
    # loop witnesses. Bound that expansion before constructing any large strings.
    estimate = 20_000 + _bytes(h(title)) * 3 + _bytes(h(graph.get("description") or "")) * 2
    for size in names.values():
        estimate += size * 6 + 1_500
    for edge in graph["edges"]:
        estimate += (names[edge["from"]] + names[edge["to"]]) * 8 + _bytes(h(edge["text"])) * 6 + 2_500
    if estimate > MAX_OUTPUT_BYTES:
        fail("OUTPUT_LIMIT", "Expanded appendix exceeds the 16 MiB safety budget; split the diagram or shorten "
             "repeated labels.")


def render_appendix(graph: dict, title: str | None = None, lang: Any = "en", label_lang: str | None = None,
                    fragment: Any = False, id_prefix: Any = _DEFAULT) -> str:
    """Render a reviewed graph (see ``apply_review``) as a standalone page or an embeddable fragment."""
    tag, _, t = interface_language(lang)
    source_lang = None if label_lang is None else language_tag(label_lang, "Source text language")
    if id_prefix is _DEFAULT:
        id_prefix = f"mp-{graph['sourceSha256'][:12]}"
    authored_title = title if title is not None else graph.get("title")
    title = plain_text(authored_title or t("fallbackTitle"), "Title", 2_000)
    if not isinstance(fragment, bool):
        fail("OPTIONS", "fragment must be a boolean.")
    if not isinstance(id_prefix, str) or not _ID_PREFIX.fullmatch(id_prefix):
        fail("ID_PREFIX", "ID prefix must begin with a letter and contain only letters, digits, underscores, or "
             "hyphens (64 characters maximum).")
    if not isinstance(graph.get("reviewed"), bool):
        fail("REVIEW_REQUIRED", "Call apply_review before rendering an appendix.")
    _check_expansion_budget(graph, title)

    bdi_open = f'<bdi lang="{h(source_lang)}">' if source_lang else "<bdi>"

    def original(value: str) -> str:
        return f"{bdi_open}{h(value)}</bdi>"

    nodes = {node["id"]: {**node, "anchor": f"{id_prefix}-node-{i + 1}", "number": i + 1}
             for i, node in enumerate(graph["nodes"])}
    edges = {edge["id"]: edge for edge in graph["edges"]}
    base = 2 if fragment else 1

    def heading(level: int, element_id: str, text: str) -> str:
        return f'<h{level} id="{element_id}" tabindex="-1">{text}</h{level}>'

    def name(node_id: str) -> str:
        node = nodes[node_id]
        return f"{original(node['label'])} <code>({original(node['id'])})</code>"

    def ref(node_id: str) -> str:
        return f'<a href="#{nodes[node_id]["anchor"]}">{name(node_id)}</a>'

    def edge_text(edge: dict) -> str:
        return t("unlabeled") if edge["textOrigin"] == "placeholder" else original(edge["text"])

    def pending_text(edge: dict) -> str:
        return "" if edge["confirmed"] else f" ({t('unconfirmed')})"

    def edge_description(edge: dict) -> str:
        return edge_text(edge) + pending_text(edge)

    def loop_ref(loop_id: str) -> str:
        return f'<a href="#{id_prefix}-{loop_id}">{t("loopTitle", {"number": h(loop_id[5:])})}</a>'

    def relationship(edge: dict) -> str:
        if edge["from"] == edge["to"]:
            return t("selfReturn", {"loop": loop_ref(edge["loop"])})
        if edge["loop"]:
            return t("loopReturn", {"loop": loop_ref(edge["loop"]), "target": ref(edge["to"]),
                                    "source": ref(edge["from"])})
        return t("oneWay")

    def connection(edge: dict) -> str:
        return t("connection", {"source": ref(edge["from"]), "text": edge_description(edge),
                                "target": ref(edge["to"])})

    node_items = []
    for node in graph["nodes"]:
        shape_html = f'<span lang="en">{h(node["shape"])}</span>' if node["shape"] else ""
        shape = f"; {t('shape', {'shape': shape_html})}" if node["shape"] else ""
        counts = t("nodeCounts", {"outgoing": len(node["outgoing"]), "incoming": len(node["incoming"])})
        node_items.append(f"<li>{ref(node['id'])} {DASH} {counts}{shape}.</li>")
    node_index = "<ol>" + "\n".join(node_items) + "</ol>"

    if graph["edges"]:
        rows = "\n".join(
            f"<tr><td>{ref(e['from'])}</td><td>{edge_description(e)} <code>[{h(e['id'])}]</code></td>"
            f"<td>{ref(e['to'])}</td><td>{t(e['style'])}</td><td>{relationship(e)}</td></tr>"
            for e in graph["edges"])
        adjacency = (f"<table>\n<caption>{t('tableCaption')}</caption>\n"
                     f'<thead><tr><th scope="col">{t("from")}</th><th scope="col">{t("text")}</th>'
                     f'<th scope="col">{t("to")}</th><th scope="col">{t("lineStyle")}</th>'
                     f'<th scope="col">{t("relationship")}</th></tr></thead>\n'
                     f"<tbody>{rows}</tbody></table>")
    else:
        adjacency = f"<p>{t('noConnections')}</p>"

    path_sections = []
    for node in graph["nodes"]:
        n = nodes[node["id"]]
        if n["outgoing"]:
            lead = (t("multipleOutgoing", {"count": len(n["outgoing"])}) if len(n["outgoing"]) > 1
                    else t("oneOutgoing"))
            choices_items = "\n".join(
                f'<li><a href="#{nodes[edges[i]["to"]]["anchor"]}">'
                f'{t("choice", {"text": edge_description(edges[i]), "target": name(edges[i]["to"])})}</a>. '
                f"{relationship(edges[i])}</li>" for i in n["outgoing"])
            choices = f"<p>{lead}</p><ul>{choices_items}</ul>"
        else:
            choices = f"<p>{t('noOutgoing')}</p>"
        if n["incoming"]:
            incoming_items = "\n".join(
                f"<li>{t('incoming', {'source': ref(edges[i]['from']), 'text': edge_description(edges[i])})}</li>"
                for i in n["incoming"])
            incoming = f"<p>{t('incomingIntro')}</p><ul>{incoming_items}</ul>"
        else:
            incoming = f"<p>{t('noIncomingSentence')}</p>"
        belongs = f"<p>{t('belongsTo', {'loop': loop_ref(n['loop'])})}</p>" if n["loop"] else ""
        path_sections.append(
            f"<section>{heading(base + 2, n['anchor'], t('nodeTitle', {'number': n['number'], 'name': name(n['id'])}))}"
            f"{belongs}{choices}{incoming}<p><a href=\"#{id_prefix}-nodes\">{t('returnNodeList')}</a></p></section>")
    path_nodes = "\n".join(path_sections)

    if graph["loops"]:
        loop_sections = []
        for loop in graph["loops"]:
            members = "".join(f"<li>{ref(member)}</li>" for member in loop["nodes"])
            inside_items = []
            for i in loop["edges"]:
                edge = edges[i]
                if edge["from"] == edge["to"]:
                    back = t("selfReturnShort")
                else:
                    back = t("returnPath", {"target": ref(edge["to"]), "source": ref(edge["from"])})
                inside_items.append(f"<li>{connection(edge)} {back}</li>")
            inside = "\n".join(inside_items)
            witness = "\n".join(f"<li>{connection(edges[i])}</li>" for i in loop["witness"])
            loop_anchor = f"{id_prefix}-{loop['id']}"
            loop_heading = heading(base + 2, loop_anchor, t("loopTitle", {"number": h(loop["id"][5:])}))
            loop_sections.append(
                f"<section>{loop_heading}"
                f"<p>{t('loopExplanation')}</p><ul>{members}</ul><p>{t('loopConnections')}</p><ul>{inside}</ul>"
                f"<p>{t('returnExample')}</p><ol>{witness}</ol>"
                f"<p>{t('routeReturns', {'node': ref(edges[loop['witness'][0]]['from'])})}</p></section>")
        loops = "\n".join(loop_sections)
    else:
        loops = f"<p>{t('noLoops')}</p>"

    if graph["reviewed"]:
        status = f"<p>{t('confirmed', {'count': len(graph['edges'])})}</p>"
    else:
        draft_text = t("draft", {"count": len(graph["unconfirmed"])})
        status = f'<p class="draft"><strong>{draft_text}</strong></p>'
    description = f"<p>{original(graph['description'])}</p>" if graph.get("description") else ""
    branches = sum(1 for node in graph["nodes"] if len(node["outgoing"]) > 1)
    summary = t("summary", {"nodes": len(graph["nodes"]), "edges": len(graph["edges"]), "branches": branches,
                            "loops": len(graph["loops"])})
    entries = "\n".join(
        f"<li>{ref(entry['node'])} {DASH} "
        f"{t('loopCandidate' if entry['kind'] == 'loop-entry-candidate' else 'noIncoming')}.</li>"
        for entry in graph["entries"])
    if graph["warnings"]:
        notes = "".join(
            "<li>" + (t(WARNING_KEYS[w]) if w in WARNING_KEYS else f'<span lang="en">{h(w)}</span>') + "</li>"
            for w in graph["warnings"])
        authoring = f"<section><h{base + 1}>{t('authoringNotes')}</h{base + 1}><ul>{notes}</ul></section>"
    else:
        authoring = ""
    title_html = original(title) if authored_title else h(title)
    nav = (f'<nav aria-label="{t("navLabel")}: {h(title)}"><ul>'
           f'<li><a href="#{id_prefix}-entries">{t("entries")}</a></li>'
           f'<li><a href="#{id_prefix}-nodes">{t("nodeListLink")}</a></li>'
           f'<li><a href="#{id_prefix}-adjacency">{t("adjacency")}</a></li>'
           f'<li><a href="#{id_prefix}-paths">{t("paths")}</a></li>'
           f'<li><a href="#{id_prefix}-loops">{t("loops")}</a></li></ul></nav>')
    body = (
        f'<section class="mermaid-paths" lang="{h(tag)}" aria-labelledby="{id_prefix}-title">\n'
        f"{heading(base, f'{id_prefix}-title', title_html)}\n"
        f"{status}\n"
        f"{description}\n"
        f"<p>{summary}</p>\n"
        f"{nav}\n"
        f"<p>{t('instructions')}</p>\n"
        f"<section>{heading(base + 1, f'{id_prefix}-entries', t('entries'))}<p>{t('entryIntro')}</p><ul>{entries}</ul></section>\n"
        f"<section>{heading(base + 1, f'{id_prefix}-nodes', t('nodes'))}<p>{t('nodeIntro')}</p>{node_index}</section>\n"
        f"<section>{heading(base + 1, f'{id_prefix}-adjacency', t('adjacency'))}{adjacency}</section>\n"
        f"<section>{heading(base + 1, f'{id_prefix}-paths', t('paths'))}{path_nodes}</section>\n"
        f"<section>{heading(base + 1, f'{id_prefix}-loops', t('loops'))}{loops}</section>\n"
        f"{authoring}\n"
        f"</section>")
    if fragment:
        result = f"{body}\n"
    else:
        title_lang = f' lang="{h(source_lang)}"' if authored_title and source_lang else ""
        result = (f'<!DOCTYPE html>\n<html lang="{h(tag)}"><head><meta charset="utf-8"><meta name="viewport" '
                  f'content="width=device-width, initial-scale=1"><title{title_lang}>{h(title)}</title>'
                  f"<style>{APPENDIX_STYLES}</style></head><body><main>{body}</main></body></html>\n")
    if _bytes(result) > MAX_OUTPUT_BYTES:
        fail("OUTPUT_LIMIT", "Rendered appendix exceeds 16 MiB; split the diagram.")
    return result


def styles_text() -> str:
    """``appendixStyles.trimStart()``: the stylesheet the ``styles`` command writes."""
    return APPENDIX_STYLES.lstrip(JS_WHITESPACE)
