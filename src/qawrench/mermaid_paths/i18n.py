"""Interface messages (English and Simplified Chinese) and BCP 47 language tags."""
from __future__ import annotations

import re
from typing import Callable

from ._js import utf16_len
from .errors import fail

MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        "fallbackTitle": "Flowchart path appendix",
        "confirmed": "All {count} connection descriptions have been confirmed by the author.",
        "draft": "Draft: {count} connection description(s) have not been confirmed by the author.",
        "summary": "{nodes} nodes; {edges} directed connections; {branches} branch points; {loops} loop groups.",
        "navLabel": "Appendix sections", "entries": "Entry candidates", "nodes": "Semantic node list",
        "nodeListLink": "Node list", "adjacency": "Branch adjacency table", "paths": "Follow a path",
        "loops": "Loops and returns",
        "instructions": "Use Tab and Shift+Tab to move between links, and Enter to follow a connection. Use "
                        "your browser's Back command to retrace followed links. Screen-reader heading navigation "
                        "can move between nodes. Each connection goes to a named node; incoming links explore "
                        "predecessors. Loops are linked rather than expanded into endless paths.",
        "entryIntro": "These are structural starting suggestions. For a loop group with no outside incoming "
                      "connections, the first declared node is offered. Every node is also available in the "
                      "node list.",
        "loopCandidate": "candidate within a loop group", "noIncoming": "no incoming connections",
        "nodeIntro": "Labels and IDs come from the source. Shapes are reported without assigning process meaning.",
        "nodeCounts": "{outgoing} outgoing, {incoming} incoming connection(s)", "shape": "shape: {shape}",
        "tableCaption": "All directed connections, including every branch", "from": "From",
        "text": "Connection text", "to": "To", "relationship": "Relationship", "lineStyle": "Line style",
        "solid": "solid", "dotted": "dotted", "thick": "thick",
        "unconfirmed": "unconfirmed", "unlabeled": "Unlabeled connection", "oneWay": "One-way connection.",
        "selfReturn": "Returns directly to this node. {loop}.",
        "loopReturn": "{loop}: from {target}, a path returns to {source}.",
        "loopTitle": "Loop {number}",
        "multipleOutgoing": "{count} outgoing connections. Follow the author’s connection text for conditions "
                            "and actions.",
        "oneOutgoing": "One outgoing connection.", "choice": "{text} — go to {target}",
        "noOutgoing": "No outgoing connections: this is a structural endpoint.",
        "incomingIntro": "Incoming connections (use these links to explore predecessors):",
        "incoming": "From {source} — {text}.", "noIncomingSentence": "No incoming connections.",
        "returnNodeList": "Return to node list", "nodeTitle": "Node {number}: {name}",
        "belongsTo": "This node belongs to {loop}.",
        "loopExplanation": "Nodes in this group can reach each other by directed paths. This describes "
                           "connectivity, not the intended order or number of repetitions.",
        "loopConnections": "Connections that stay within this loop group:",
        "connection": "From {source} — {text} — to {target}.", "selfReturnShort": "Returns directly to itself.",
        "returnPath": "From {target}, a path returns to {source}.", "returnExample": "One example return route:",
        "routeReturns": "This route returns to {node}.", "noLoops": "No directed loops.",
        "noConnections": "No directed connections.",
        "authoringNotes": "Authoring notes",
        "noteDirectives": "Mermaid configuration directives are not executed or interpreted.",
        "noteFrontmatter": "Frontmatter configuration is not interpreted; use accTitle and accDescr for appendix "
                           "text.",
        "noteStyles": "Styles and classes are not interpreted; express meaningful distinctions in text.",
        "noteDirection": "Direction controls layout only and is not interpreted as process order.",
        "reviewInstructions": "Check every source/destination and connection description. Edit text as needed, "
                              "then set confirmed to true for each edge. Recreate this file whenever the source "
                              "changes.",
    },
    "zh-CN": {
        "fallbackTitle": "流程图路径附录",
        "confirmed": "作者已确认全部 {count} 条连接的说明文字。",
        "draft": "草稿：还有 {count} 条连接的说明文字未经作者确认。",
        "summary": "{nodes} 个节点；{edges} 条有向连接；{branches} 个分支节点；{loops} 个循环组。",
        "navLabel": "附录章节", "entries": "入口候选节点", "nodes": "语义节点列表",
        "nodeListLink": "节点列表", "adjacency": "分支邻接表", "paths": "逐节点跟随路径", "loops": "循环与返回关系",
        "instructions": "使用 Tab 和 Shift+Tab 在链接之间移动，按 Enter 跟随连接。可用浏览器的“后退”命令回到此前跟随"
                        "的位置。读屏软件的标题导航也可用于切换节点。每条连接都指向有名称的节点；入向连接链接可用于探索"
                        "前驱节点。循环通过链接表达，不会展开为无穷多条路径。",
        "entryIntro": "以下入口建议依据连接结构生成。对于没有外部入向连接的循环组，提供最先声明的节点作为候选入口。所有"
                      "节点均可从节点列表访问。",
        "loopCandidate": "循环组中的入口候选", "noIncoming": "没有入向连接",
        "nodeIntro": "节点文字和标识符来自源文件。形状名称仅用于说明形状，不据此推断流程含义。",
        "nodeCounts": "{outgoing} 条出向连接，{incoming} 条入向连接", "shape": "形状：{shape}",
        "tableCaption": "全部有向连接，包含每个分支", "from": "起点", "text": "连接文字", "to": "终点",
        "relationship": "连接关系", "lineStyle": "线条样式",
        "solid": "实线", "dotted": "虚线", "thick": "粗线",
        "unconfirmed": "未经确认", "unlabeled": "未标注文字的连接", "oneWay": "单向连接。",
        "selfReturn": "直接返回本节点。{loop}。",
        "loopReturn": "{loop}：从{target}有路径返回{source}。",
        "loopTitle": "循环组 {number}",
        "multipleOutgoing": "{count} 条出向连接。请根据作者确认的连接文字理解条件和操作要求。",
        "oneOutgoing": "一条出向连接。", "choice": "{text}——前往{target}",
        "noOutgoing": "没有出向连接：此节点在连接结构中是终点。",
        "incomingIntro": "入向连接（可通过以下链接探索前驱节点）：",
        "incoming": "来自{source}——{text}。", "noIncomingSentence": "没有入向连接。",
        "returnNodeList": "返回节点列表", "nodeTitle": "节点 {number}：{name}", "belongsTo": "本节点属于{loop}。",
        "loopExplanation": "本组内的节点可通过有向路径相互到达。这说明连接关系，不代表作者规定的执行顺序或重复次数。",
        "loopConnections": "留在本循环组内部的连接：",
        "connection": "从{source}——{text}——到{target}。", "selfReturnShort": "直接返回自身。",
        "returnPath": "从{target}有路径返回{source}。", "returnExample": "一条具体的返回路线：",
        "routeReturns": "沿此路线返回{node}。", "noLoops": "没有有向循环。", "noConnections": "没有有向连接。",
        "authoringNotes": "作者注意事项",
        "noteDirectives": "不会执行或解释 Mermaid 配置指令。",
        "noteFrontmatter": "不会解释前置配置；请使用 accTitle 和 accDescr 提供附录文字。",
        "noteStyles": "不会解释样式和类；请用文字表达有意义的区别。",
        "noteDirection": "方向仅影响绘图布局，不据此推断流程顺序。",
        "reviewInstructions": "检查每条连接的起点、终点和说明文字。按需编辑 text，确认后再把该连接的 confirmed 改为 "
                              "true。源文件发生变化后，请重新生成并确认此文件。",
    },
}

# A small subset of CLDR's alias data (the original used the JavaScript runtime's
# full ``Intl.Locale`` canonicalization): common deprecated and overlong codes.
_LANGUAGE_ALIASES = {
    "iw": "he", "in": "id", "ji": "yi", "jw": "jv", "mo": "ro", "eng": "en", "zho": "zh", "chi": "zh",
    "cmn": "zh", "ara": "ar", "deu": "de", "ger": "de", "fra": "fr", "fre": "fr", "spa": "es", "rus": "ru",
    "jpn": "ja", "kor": "ko", "por": "pt", "ita": "it", "heb": "he", "ind": "id", "nld": "nl", "dut": "nl",
}
_REGION_ALIASES = {"BU": "MM", "DD": "DE", "FX": "FR", "TP": "TL", "YD": "YE", "ZR": "CD", "UK": "GB"}
_ALPHA = re.compile(r"[A-Za-z]+")
_ALNUM = re.compile(r"[A-Za-z0-9]+")


def _canonical_tag(value: str) -> str | None:
    """Validate a Unicode BCP 47 locale identifier and return its canonical form, or None."""
    subtags = value.split("-")
    if not subtags or any(not s or not s.isascii() or not _ALNUM.fullmatch(s) for s in subtags):
        return None
    index = 0
    language = subtags[0].lower()
    if not (_ALPHA.fullmatch(language) and (2 <= len(language) <= 3 or 5 <= len(language) <= 8)):
        return None
    index = 1
    script = region = None
    variants: list[str] = []
    if index < len(subtags) and len(subtags[index]) == 4 and _ALPHA.fullmatch(subtags[index]):
        script = subtags[index].title()
        index += 1
    if index < len(subtags) and ((len(subtags[index]) == 2 and _ALPHA.fullmatch(subtags[index]))
                                 or (len(subtags[index]) == 3 and subtags[index].isdigit())):
        region = subtags[index].upper()
        index += 1
    while index < len(subtags):
        subtag = subtags[index].lower()
        if (5 <= len(subtag) <= 8) or (len(subtag) == 4 and subtag[0].isdigit()):
            if subtag in variants:
                return None
            variants.append(subtag)
            index += 1
        else:
            break
    extensions: dict[str, list[str]] = {}
    private: list[str] = []
    while index < len(subtags):
        singleton = subtags[index].lower()
        if len(singleton) != 1:
            return None
        index += 1
        if singleton == "x":
            private = [s.lower() for s in subtags[index:]]
            if not private or any(len(s) > 8 for s in private):
                return None
            index = len(subtags)
            break
        if singleton in extensions:
            return None
        values = []
        while index < len(subtags) and len(subtags[index]) > 1:
            if len(subtags[index]) > 8:
                return None
            values.append(subtags[index].lower())
            index += 1
        if not values:
            return None
        extensions[singleton] = values
    language = _LANGUAGE_ALIASES.get(language, language)
    if script == "Qaai":
        script = "Zinh"
    if region is not None:
        region = _REGION_ALIASES.get(region, region)
    parts = [language, *(p for p in (script, region) if p), *sorted(variants)]
    for singleton in sorted(extensions):
        parts += [singleton, *extensions[singleton]]
    if private:
        parts += ["x", *private]
    return "-".join(parts)


def language_tag(value, field: str = "Language") -> str:
    if not isinstance(value, str) or utf16_len(value) > 80:
        fail("LANGUAGE", f"{field} must be a valid BCP 47 language tag.")
    canonical = _canonical_tag(value)
    if canonical is None:
        fail("LANGUAGE", f"{field} must be a valid BCP 47 language tag, such as en or zh-CN.")
    return canonical


def interface_language(value="en") -> tuple[str, str, Callable[..., str]]:
    """(canonical tag, message key, translate function) for a supported interface language."""
    tag = language_tag(value, "Interface language")
    subtags = tag.split("-")
    language = subtags[0]
    script = region = None
    rest = subtags[1:]
    if rest and len(rest[0]) == 4 and rest[0].isalpha():
        script = rest.pop(0)
    if rest and (len(rest[0]) == 2 or (len(rest[0]) == 3 and rest[0].isdigit())):
        region = rest[0]
    key = None
    if language == "en" and (script is None or script == "Latn"):
        key = "en"
    if language == "zh" and (script == "Hans" or (script is None and (region is None or region in ("CN", "SG", "MY")))):
        key = "zh-CN"
    if key is None:
        fail("LANGUAGE", "Interface language must be English (en) or Simplified Chinese (zh-CN). Use --label-lang "
             "to tag source text in another language.")
    messages = MESSAGES[key]

    def translate(name: str, values: dict | None = None) -> str:
        values = values or {}
        return re.sub(r"\{([A-Za-z]+)\}", lambda m: "" if values.get(m.group(1)) is None else str(values[m.group(1)]),
                      messages[name])

    return tag, key, translate
