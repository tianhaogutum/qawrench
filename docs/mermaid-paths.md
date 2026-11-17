# qawrench mermaid-paths

`qawrench mermaid-paths` (previously the standalone `mermaid-paths` tool) turns a basic Mermaid flowchart
into an **author-reviewed, keyboard-followable static HTML appendix**: a semantic
node list, a branch adjacency table, linked node-by-node paths and explicit
loop/return routes. Use it when a published flowchart must also be readable without
seeing the SVG, for example in accessible documentation, and you want every
connection's wording confirmed by its author before it ships.

The generated HTML works offline and contains no JavaScript. No browser, server,
network request, model or Mermaid rendering is involved.

## Install

Requires Python 3.11 or newer. This command has no runtime dependencies beyond the
standard library, so no extra is needed.

```sh
pipx install qawrench                 # or: pip install qawrench
qawrench mermaid-paths --version      # prints the bare qawrench version, e.g. 0.1.0

# Or run without installing:
uvx qawrench mermaid-paths --help
```

`python -m qawrench mermaid-paths` is equivalent to `qawrench mermaid-paths`. Options
follow the subcommand (`qawrench mermaid-paths build INPUT --draft`, not
`qawrench mermaid-paths --draft build INPUT`).

## Quick start

The examples use files from a qawrench source checkout; run them from its root.
`examples/mermaid-paths/review-process.review.json` has already been confirmed by
the example's author, so this regenerates the committed
`examples/mermaid-paths/review-process.html` byte for byte:

```sh
qawrench mermaid-paths build examples/mermaid-paths/review-process.mmd \
  --review examples/mermaid-paths/review-process.review.json \
  --label-lang en -o /tmp/review-process.html
```

For your own diagram, create a review template, let the author check it, then build:

```sh
qawrench mermaid-paths review workflow.mmd -o workflow.review.json
# Check endpoint labels, sourceLine, lineStyle and sourceLabel for every edge.
# Edit text, then set confirmed to true only after checking that edge.
qawrench mermaid-paths build workflow.mmd --review workflow.review.json -o workflow.html
```

To look at unconfirmed output, or at the analysed graph:

```sh
qawrench mermaid-paths build workflow.mmd --draft -o workflow.draft.html
qawrench mermaid-paths inspect workflow.mmd -o workflow.graph.json
cat workflow.mmd | qawrench mermaid-paths build - --draft > workflow.draft.html
```

## Commands and options

| Command | Purpose | Options |
| --- | --- | --- |
| `review INPUT` | Write the review template (`mermaid-paths/review-v1` JSON) | `-o/--output`, `--force`, `--lang en\|zh-CN`, `--markdown`, `--diagram N` |
| `inspect INPUT` | Write the analysed graph (`mermaid-paths/graph-v1` JSON) | `-o/--output`, `--force`, `--markdown`, `--diagram N` |
| `build INPUT` | Write the HTML appendix | `--review REVIEW` or `--draft`, `-o/--output`, `--force`, `--title TEXT`, `--lang en\|zh-CN`, `--label-lang TAG`, `--fragment`, `--id-prefix PREFIX`, `--markdown`, `--diagram N` |
| `styles` | Write the appendix stylesheet for embedded fragments | `-o/--output`, `--force` |

`INPUT` and `--output` may be `-` for stdin/stdout; output defaults to stdout. The
review file must be a real file (stdin is reserved for the Mermaid source). Input
must be UTF-8; a byte-order mark is kept as part of the reviewed bytes.

File output is atomic and refuses to replace an existing file unless `--force` is
given. Even with `--force`, the output may never replace the Mermaid source or the
review input, including through symlinks or other names for the same file.

Exit status: **0** success, **2** invalid arguments, input or review (the message is
`qawrench mermaid-paths: CODE: message`, with codes such as `PARSE`,
`UNSUPPORTED_EDGE`, `STALE_REVIEW` or `UNCONFIRMED`), **1** filesystem or unexpected
failures.

## The author review

No label is guessed from the diagram's layout. An unlabeled edge initially says
`Unlabeled connection`; authors should provide useful connection text. Even
already-labeled edges require confirmation. Each review record carries `id`, `from`,
`fromLabel`, `to`, `toLabel`, `sourceLine`, `lineStyle`, `sourceLabel`, `text` and
`confirmed`; the context fields are validated whenever they are present, and older
templates without them are still accepted.

Review files bind to the SHA-256 of the exact diagram source bytes and to the complete
expanded edge list. Any raw source change, including whitespace, requires a fresh
review (`STALE_REVIEW`). This is an author attestation, not an identity or
cryptographic signature system.

`--draft` renders unconfirmed connections, visibly marked as a draft, and announces
"unconfirmed" in every link name and return route that uses them. Malformed or stale
review files still fail in draft mode, and an explicitly supplied `null` review is
rejected. With zero edges there is no connection text to confirm.

## Reading and embedding

Readers use ordinary Tab/Shift+Tab and Enter to follow links and the browser's Back
command to retrace them; screen-reader heading navigation also reaches every node.
Targets are headings with `tabindex="-1"`, and focus/target outlines stay visible.
Incoming links expose predecessors. Every node is reachable from the node list,
including disconnected nodes and cycles with no outside entry.

Entry candidates come from graph connectivity: a loop group with no outside incoming
connection offers its first declared node. Outgoing branches are listed without
inferring whether they are alternatives or parallel actions. Strongly connected loop
groups state exactly where a return path exists, and each group includes one concrete
example route back to its starting node. Routes are linked, never enumerated.

For an existing document, emit a fragment and put the CSS in its head:

```sh
qawrench mermaid-paths build workflow.mmd --review workflow.review.json \
  --fragment --id-prefix publishing-flow -o appendix.fragment.html
qawrench mermaid-paths styles -o appendix.css
```

Include `<link rel="stylesheet" href="appendix.css">` in the host document's head and
the fragment in its body. Fragments use h2/h3/h4; standalone pages use h1/h2/h3. Give
each embedded copy a distinct `--id-prefix` (letters, digits, `_`, `-`; it must start
with a letter; at most 64 characters) and distinct diagrams distinct `--title` values
so navigation landmarks are clearly named. Keep Mermaid's `accTitle`/`accDescr` on the
visual diagram and link its nearby text to the appendix; the appendix does not alter
the SVG.

## Languages

Interface text is available in English (`en`, `en-GB`, …) and Simplified Chinese
(`zh`, `zh-CN`, `zh-Hans`, …); `--lang` rejects any other interface language. Source
labels and author-written descriptions are never translated. When they are in another
language, pass `--label-lang` with a BCP 47 tag (such as `ar` or `de`); source text is
isolated with native `bdi` elements so bidirectional labels stay intact. One tag
applies to all source and review text.

```sh
qawrench mermaid-paths review workflow.mmd --lang zh-CN -o workflow.review.json
qawrench mermaid-paths build workflow.mmd --review workflow.review.json \
  --lang zh-CN --label-lang zh-CN -o workflow.html
```

`examples/mermaid-paths/approval-zh.html` is a complete Chinese example; a Chinese
version of this page is in [mermaid-paths.zh-CN.md](mermaid-paths.zh-CN.md).

## Markdown documents

`--markdown` reads Mermaid code fences from a Markdown document instead of a raw
`.mmd` file. Several Mermaid diagrams require an explicit 1-based `--diagram N`; the
command never silently picks the first one and never changes the Markdown file.

```sh
qawrench mermaid-paths build examples/mermaid-paths/workflow-doc.md --markdown \
  --review examples/mermaid-paths/review-process.review.json -o /tmp/workflow.html
```

Fences are found with Marked's block rules (GFM): backtick and tilde fences, including
fences nested in lists and block quotes; only fences whose info string's first word
is `mermaid` (any case) count; indented code, inline code and HTML comments do not.
Fence content is de-indented and line endings are normalized, and a terminal line
feed is added. The review then binds to that extracted diagram text, so editing the
surrounding prose does not invalidate it, and `sourceLine` is relative to the
diagram. The source and complexity limits apply to the whole Markdown document.

## Supported Mermaid subset

The command reads Mermaid flowchart syntax with a hand-written reader that follows
Mermaid 11's flowchart lexer rules (as specified by mmd-toolkit), and accepts only
what a static path appendix can represent faithfully:

- **Diagrams.** A `flowchart`, `graph`, `flowchart-elk` or `swimlane-beta` header,
  with an optional direction (`TB`, `TD`, `BT`, `RL`, `LR`, and Mermaid's `BR`, `v`,
  `<`, `>`, `^`) followed by `;` or a line break. Statements are separated by line
  breaks or `;`. Other diagram types fail with `DIAGRAM_TYPE`; text with no diagram
  header fails with `PARSE` ("No diagram type detected").
- **Nodes.** Bare IDs (ASCII letters and digits, `_ - . ! # $ % & ' * + / ? \` and
  backquote, `:`/`,` inside an ID, the word `default`, and the Unicode letters of
  Mermaid's letter table) with an optional shape and label: `[ ]` rectangle, `( )`
  rounded, `([ ])` stadium, `[[ ]]` framed-rectangle, `[( )]` cylinder, `(( ))`
  circle, `((( )))` double-circle, `> ]` odd, `{ }` diamond, `{{ }}` hexagon,
  `[/ /]` lean-right, `[\ \]` lean-left, `[/ \]` trapezoid-bottom, `[\ /]`
  trapezoid-top, `(- -)` ellipse and `[|key:value| ]` rectangle. Repeated
  declarations are allowed; the last explicit label wins. Shape names are reported
  without assigning process meaning (a diamond is not called a decision). Mermaid
  keywords (`graph`, `flowchart`, `subgraph`, `end`, `style`, `class`, `classDef`,
  `linkStyle`, `interpolate`, `click`, `_self`, `_blank`, `_parent`, `_top`, …) cannot
  be node IDs.
- **Connections.** One-way arrows only: solid `-->` (and longer, `--->`), thick
  `==>`, dotted `-.->` (`-..->`), with labels as `-->|text|` or inline
  `-- text -->`, `== text ==>`, `-. text .->`; an optional edge ID (`A e1@--> B`) is
  kept as `authoredId`. Chains (`A --> B --> C`) and fan-out with spaced `&`
  (`A & B --> C & D`) expand to every directed pair; parallel edges, self-loops and
  larger cycles are kept.
- **Text.** Plain, `"quoted"` and Markdown (`` "`**bold**`" ``) labels; Mermaid
  entities such as `#quot;` and `#9829;`; named HTML entities such as `&amp;`; basic
  HTML formatting and image `alt` text. `accTitle:`, `accDescr:` and `accDescr { … }`
  become the appendix title and description (the block must close with `}`).
- **Accepted but not interpreted** (listed under "Authoring notes" in the appendix):
  `direction` lines, `style`, `classDef`, `class`, `linkStyle`, `:::class`,
  `%%{…}%%` directives and YAML frontmatter. Whole-line `%%` comments (with text after
  the `%%`) are ignored.
- **Rejected explicitly.** `UNSUPPORTED_EDGE` for unheaded, bidirectional, circle,
  cross and invisible links (`---`, `<-->`, `--o`, `--x`, `o--o`, `x--x`, `~~~`);
  `UNSUPPORTED` for `subgraph … end`, `click …` and `@{ … }` node or edge data;
  `ENTITY_AMBIGUITY` for numeric HTML entities (`&#9829;`, `&#x2665;`; write Mermaid's
  `#9829;` instead); `NON_TEXT_LABEL` for hidden, styled, class-based or
  ARIA-dependent label markup, embedded SVG/MathML/media, lists and tables (also
  Markdown lists/tables, and Markdown code fences with a language), and images without
  alt text; `EMPTY_LABEL`, `UNTERMINATED_METADATA` and `EMPTY_GRAPH`. Anything else
  Mermaid's lexer and grammar do not accept fails with `PARSE` and a line:column.

Two Mermaid lexer rules surprise authors and are kept on purpose: a `%%` comment must
occupy its whole line (`A --> B %% note` is a parse error), and a line that contains
`direction` followed by `TB`, `BT`, `RL`, `LR` or `TD` anywhere, even inside a quoted
label, is read as a direction statement in full; avoid that phrase in labels. HTML links in labels become plain text; icon/math shorthand such as
`fa:fa-ban` stays literal text. No visual layout or image content is interpreted.

## Limits

Hard limits: 1 MiB of source, 1,000 nodes, 5,000 expanded edges, 20,000 characters per
source label or identifier, 2,000 per review description and per title, and 16 MiB
per review file. A pre-parse budget of 50,000 whitespace/punctuation/symbol
characters (label text included) bounds pathological inputs. Conservative 16 MiB HTML
and JSON export budgets reject excessive repeated text before large strings are built
or any output is written.

Assistive-technology and browser combinations have **not** been manually tested;
static semantics and HTML structure are tested. No WCAG conformance or usability
certification is claimed.

## Library and tests

```python
from qawrench.mermaid_paths import (APPENDIX_STYLES, apply_review, create_review,
                                    parse_flowchart, render_appendix, select_mermaid_block)

graph = parse_flowchart(source)
template = create_review(graph)              # give to the author; do not auto-confirm
reviewed = apply_review(graph, author_review)
html = render_appendix(reviewed, fragment=True, id_prefix="my-flow")
# Put APPENDIX_STYLES in the host document's stylesheet/head.
block = select_mermaid_block(markdown_text, diagram=2)   # {"index", "source", "sourceSha256"}
```

`extract_mermaid_blocks(markdown)` lists every Mermaid fence; `source_hash(text)` and
`LIMITS` are exported too. Errors are `AppendixError` with a `code` attribute. The
`graph-v1` and `review-v1` JSON formats, including the `mermaid-paths/…` schema
identifiers, are unchanged from the standalone tool, so existing review files keep
working.

The tests in `tests/mermaid_paths/` use an offline corpus of 110 real examples from
the Mermaid 11.12.0 flowchart documentation: the 44 supported ones must match graphs
recorded from Mermaid's official parser (also matched by Mermaid 12.3.0), and the 66
outside the subset must fail with `UNSUPPORTED` or `UNSUPPORTED_EDGE`. Further tests
cover branch/cycle integrity on 100 deterministic mixed-ID graphs, review tampering,
entities, injection, link targets, HTML structure, UTF-8 failures, atomic output,
source-file preservation and byte-identical regeneration of the committed examples.
See [tests/mermaid_paths/fixtures/README.md](../tests/mermaid_paths/fixtures/README.md)
for the corpus provenance.

## Prior art

Keyboard navigation for Mermaid flowcharts already exists; this command covers the
narrower gap of a review-gated static appendix that does not depend on the rendered
diagram or on reader-side JavaScript. Checked in November 2026:

| Tool | Relation |
| --- | --- |
| [Mermaid accessibility options](https://mermaid.js.org/config/accessibility.html) | `accTitle`/`accDescr` describe the SVG; keep them, and add linked nodes and labeled relationships here. |
| [@inclunet/mermaid-a11y](https://github.com/inclunet/mermaid-a11y) 0.3.0 | Keyboard traversal over Mermaid's rendered SVG; use it for in-diagram navigation. It needs a DOM/SVG and has no author-confirmation step or static export. |
| [@mmd-toolkit/parser](https://mmd-toolkit.github.io/mmd-toolkit/parser/) 0.3.1 | Position-aware Mermaid parser used by the original Node tool; its flowchart rules are reimplemented here. |
| [docs-overlay-mermaid](https://github.com/hebus/docs-overlay/tree/main/packages/adapters/mermaid) 0.3.1 | DOM-free SVG renderer; its generated description omitted edge labels such as Yes/No, and it has no appendix or review workflow. |
| [@mermaid-js/parser](https://github.com/mermaid-js/mermaid/tree/develop/packages/parser) 1.2.1 | Flowcharts are not among its supported diagram types. |
| [WAI-ARIA APG link pattern](https://www.w3.org/WAI/ARIA/apg/patterns/link/) | Native links activated with Enter; hence ordinary links and headings rather than a custom graph widget or key map. |

Process roles are never inferred from shapes, direction, styles or layout. Following
the APG landmark guidance, the appendix and its navigation are named landmarks, while
individual node and loop sections use ordinary headings.

## Upstream notes

- **Numeric HTML entities in Mermaid 11.12.0.** Through `mermaidAPI.getDiagramFromText()`
  and Mermaid's own `decodeEntities()`, `A["&#9829;"]` became `&♥` while `A["#9829;"]`
  became `♥`, because the entity encoder also rewrites a `#9829;` already preceded by
  `&` (hex references are affected differently). The command therefore rejects
  numeric HTML entities with `ENTITY_AMBIGUITY` and recommends Mermaid's decimal form.
  This was recorded as a local issue draft only; it is not a claim about every Mermaid
  version.
- **graphlib 4.0.7 and prototype-like IDs.** In the original Node tool, node IDs such
  as `__proto__` crashed `@dagrejs/graphlib`'s `setEdge`, so IDs were mapped to safe
  keys at the graphlib boundary. qawrench computes strongly connected components and
  return routes itself (reproducing graphlib's shortest-path tie-breaking) with plain
  Python dictionaries; such IDs are ordinary nodes and never become HTML IDs.

## License

MIT, like the rest of qawrench. The Mermaid documentation examples in
`tests/mermaid_paths/fixtures/upstream/` are © Knut Sveidqvist and Mermaid
contributors under the MIT License (`licenses/mermaid-paths/LICENSE.mermaid`). See
[THIRD_PARTY.md](../THIRD_PARTY.md).
