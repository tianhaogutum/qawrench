# Fixture provenance

`upstream/flowchart-*.mmd` are actual examples from the Mermaid **11.12.0**
flowchart documentation, extracted with Marked 18.2.0. The full version-pinned
URL, source digest, per-fixture digests, and explicit scope decisions are recorded
in `upstream/manifest.json`. The upstream MIT license is included as
`upstream/LICENSE.mermaid` (also kept in `licenses/mermaid-paths/`).

`upstream/expected.json` contains node labels and directed edges obtained by
running the official Mermaid 11.12.0 parser (`mermaidAPI.getDiagramFromText()`,
with jsdom 26.1.0 providing DOMPurify's DOM requirements). No SVG rendering or
browser process was used. Entity placeholders in Mermaid's DB were decoded with
its own version-pinned decoder; Markdown/HTML text was then normalized for
comparison. Independent core tests specify expected Unicode entity output
explicitly.

44 examples are within the basic-node/one-way-arrow subset. 66 cover unsupported
constructs such as subgraphs, click actions, data blocks, and undirected links;
tests require explicit failures for these, rather than silent omission.

The same 44 supported fixtures were also compared with official Mermaid 12.3.0
and matched. The oracle runs were made once, when the corpus was recorded; the
Node scripts and Mermaid/jsdom installations used for them are not part of
qawrench, and the tests never fetch the network or install anything.

`examples/mermaid-paths/review-process.mmd` is an original publishing workflow
example authored for this project. Its confirmed review file demonstrates the
author workflow; it is not evidence of assistive-technology user testing.
