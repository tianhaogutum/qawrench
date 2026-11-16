import sys

from qawrench.cli import main

raise SystemExit(main(["structured-data", *sys.argv[1:]]))
