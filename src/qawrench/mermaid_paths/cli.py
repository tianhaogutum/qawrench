"""Command-line interface: review, inspect, build and styles. All processing is offline."""
from __future__ import annotations

import argparse
import errno
import json
import os
import re
import sys
import uuid

from . import __version__
from .appendix import render_appendix, styles_text
from .diagram import LIMITS, parse_flowchart
from .errors import AppendixError
from .jsonout import MAX_JSON_BYTES, json_output
from .markdown import MAX_SAFE_INTEGER, select_mermaid_block
from .review import apply_review, create_review

PROG = "qawrench mermaid-paths"
DESCRIPTION = """\
Author-reviewed, keyboard-followable static HTML appendices for Mermaid flowcharts.

  qawrench mermaid-paths review INPUT.mmd [-o review.json]
  qawrench mermaid-paths inspect INPUT.mmd [-o graph.json]
  qawrench mermaid-paths build INPUT.mmd --review review.json [-o appendix.html]
  qawrench mermaid-paths build INPUT.mmd --draft [-o draft.html]
  qawrench mermaid-paths styles [-o appendix.css]
"""
EPILOG = """\
Use --markdown to read Mermaid code fences from a Markdown document, and
--diagram N (1-based) when the document has multiple Mermaid diagrams.
INPUT or OUTPUT may be - for stdin/stdout. Output defaults to stdout.
File output refuses to overwrite existing files; use --force when intentional.
Exit status: 0 success, 2 invalid arguments/input/review, 1 filesystem or unexpected failure.
No server, JavaScript, network request, or Mermaid rendering is required.
"""


def _limited_read(path: str, limit: int) -> str:
    name = "Standard input" if path == "-" else os.path.basename(path)
    chunks = []
    total = 0
    stream = sys.stdin.buffer if path == "-" else open(path, "rb")
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise AppendixError("LIMIT", f"{name} exceeds {limit} bytes.")
            chunks.append(chunk)
    finally:
        if path != "-":
            stream.close()
    try:
        # Keep a source BOM so the review hash really binds to the exact UTF-8 bytes.
        return b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError:
        raise AppendixError("ENCODING", "Input must be valid UTF-8.") from None


def _real(path: str) -> str | None:
    try:
        return os.path.realpath(path, strict=True)
    except FileNotFoundError:
        return None


def _write_output(path: str, content: str, force: bool, protected: list[str | None]) -> None:
    data = content.encode("utf-8")
    if path == "-":
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
        return
    destination = os.path.abspath(path)
    existing = _real(destination)
    for protected_path in protected:
        if not protected_path or protected_path == "-":
            continue
        protected_real = os.path.realpath(protected_path, strict=True)
        same_file = existing is not None and os.path.samefile(existing, protected_real)
        if destination == os.path.abspath(protected_path) or existing == protected_real or same_file:
            raise AppendixError("OUTPUT_INPUT", "Output must not replace the Mermaid source or the review input.")
    temporary = os.path.join(os.path.dirname(destination), f".{os.path.basename(destination)}.{uuid.uuid4()}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        if force:
            os.replace(temporary, destination)
        else:
            os.link(temporary, destination)
    except FileExistsError:
        raise AppendixError("OUTPUT_EXISTS", "Output already exists; choose another path or use --force.") from None
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _load_review(path: str):
    text = _limited_read(path, MAX_JSON_BYTES)
    if text.startswith("\ufeff"):
        text = text[1:]

    def reject_constant(name):
        raise ValueError(f"Unexpected token {name!r}")

    try:
        review = json.loads(text, parse_constant=reject_constant)
    except ValueError as error:
        raise AppendixError("REVIEW_JSON", f"Invalid review JSON: {error}") from None
    if review is None:
        raise AppendixError("REVIEW_SCHEMA", "A supplied review JSON file must be an object, not null.")
    return review


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG, description=DESCRIPTION, epilog=EPILOG,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    def add_source(sub):
        sub.add_argument("input", metavar="INPUT", help="Mermaid source (.mmd), Markdown with --markdown, or - for stdin")
        sub.add_argument("--markdown", action="store_true", help="read Mermaid code fences from a Markdown document")
        sub.add_argument("--diagram", metavar="N", help="1-based Mermaid fence to use (requires --markdown)")

    def add_output(sub, default_hint):
        sub.add_argument("-o", "--output", metavar="OUTPUT", help=f"output file or - (default: stdout; {default_hint})")
        sub.add_argument("--force", action="store_true", help="replace an existing output file")

    review = commands.add_parser("review", help="write an author review template (JSON) for every connection",
                                 description="Write an author review template: one record per directed connection.")
    add_source(review)
    add_output(review, "review JSON")
    review.add_argument("--lang", metavar="en|zh-CN", help="language of the review instructions and unlabeled placeholder")

    inspect = commands.add_parser("inspect", help="write the analysed graph (JSON)",
                                  description="Write the analysed graph: nodes, edges, loops and entry candidates.")
    add_source(inspect)
    add_output(inspect, "graph JSON")

    build = commands.add_parser("build", help="render the HTML appendix from a confirmed review (or --draft)",
                                description="Render the static HTML appendix. Every connection must be confirmed "
                                            "in --review unless --draft is given.")
    add_source(build)
    add_output(build, "HTML")
    build.add_argument("--review", metavar="REVIEW", help="confirmed review JSON file (not stdin)")
    build.add_argument("--draft", action="store_true", help="allow unconfirmed connections; output is marked as a draft")
    build.add_argument("--title", metavar="TEXT", help="appendix title (default: accTitle or a generic title)")
    build.add_argument("--lang", metavar="en|zh-CN", help="interface language (default: en)")
    build.add_argument("--label-lang", metavar="TAG", help="BCP 47 language of source labels and review text")
    build.add_argument("--fragment", action="store_true", help="emit an embeddable fragment (h2-h4) without page or styles")
    build.add_argument("--id-prefix", metavar="PREFIX", help="prefix for generated element IDs")

    styles = commands.add_parser("styles", help="write the appendix stylesheet (CSS)",
                                 description="Write the stylesheet for embedded --fragment appendices.")
    add_output(styles, "CSS")
    return parser


def _run(args) -> None:
    output_path = args.output
    review_path = getattr(args, "review", None)
    if output_path == "" or review_path == "":
        raise AppendixError("USAGE", "Output and review paths must not be empty.")
    if args.command == "styles":
        _write_output(output_path or "-", styles_text(), args.force, [])
        return
    if review_path == "-":
        raise AppendixError("USAGE", "The review JSON must be a file; stdin is reserved for Mermaid source.")
    diagram = None
    if args.diagram is not None:
        if not args.markdown or not re.fullmatch(r"[1-9][0-9]*", args.diagram):
            raise AppendixError("USAGE", "--diagram requires --markdown and a positive integer.")
        diagram = int(args.diagram)
        if diagram > MAX_SAFE_INTEGER:
            diagram = float(diagram)
    input_text = _limited_read(args.input, LIMITS["sourceBytes"])
    source = select_mermaid_block(input_text, diagram=diagram)["source"] if args.markdown else input_text
    graph = parse_flowchart(source)
    if args.command == "review":
        output = json_output(create_review(graph, lang=args.lang if args.lang is not None else "en"))
    elif args.command == "inspect":
        output = json_output(graph)
    else:
        review = _load_review(review_path) if review_path else None
        reviewed = apply_review(graph, review, draft=args.draft)
        options = {"title": args.title, "lang": args.lang if args.lang is not None else "en",
                   "label_lang": args.label_lang, "fragment": args.fragment}
        if args.id_prefix is not None:
            options["id_prefix"] = args.id_prefix
        output = render_appendix(reviewed, **options)
    _write_output(output_path or "-", output, args.force, [args.input, review_path])


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        _run(args)
    except BrokenPipeError:
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except OSError:
            pass
        return 0
    except AppendixError as error:
        sys.stderr.write(f"{PROG}: {error.code}: {error.message}\n")
        return 2
    except OSError as error:
        code = errno.errorcode.get(error.errno or 0, "ERROR")
        sys.stderr.write(f"{PROG}: {code}: {error}\n")
        return 1
    except (RecursionError, MemoryError) as error:
        sys.stderr.write(f"{PROG}: ERROR: {type(error).__name__}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
