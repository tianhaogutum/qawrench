import sys

from qawrench.cli import main

raise SystemExit(main(["mermaid-paths", *sys.argv[1:]]))
