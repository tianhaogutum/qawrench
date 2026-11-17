"""Author-reviewed, keyboard-followable static HTML appendices for Mermaid flowcharts.

Public API (the equivalent of the original ES module exports)::

    graph = parse_flowchart(source)
    template = create_review(graph)            # give to the author; do not auto-confirm
    reviewed = apply_review(graph, author_review)
    html = render_appendix(reviewed, fragment=True, id_prefix="my-flow")
    # Put APPENDIX_STYLES in the host document's stylesheet/head.
"""
from qawrench import __version__

from .appendix import APPENDIX_STYLES, render_appendix
from .diagram import LIMITS, parse_flowchart, source_hash
from .errors import AppendixError
from .markdown import extract_mermaid_blocks, select_mermaid_block
from .review import apply_review, create_review

__all__ = [
    "APPENDIX_STYLES", "AppendixError", "LIMITS", "__version__", "apply_review", "create_review",
    "extract_mermaid_blocks", "parse_flowchart", "render_appendix", "select_mermaid_block", "source_hash",
]
