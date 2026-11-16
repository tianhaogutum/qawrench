import sys

from qawrench.cli import main

raise SystemExit(main(["page-diff", *sys.argv[1:]]))
