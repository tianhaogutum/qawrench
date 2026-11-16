import sys

from qawrench.cli import main

raise SystemExit(main(["trace-budget", *sys.argv[1:]]))
