import sys

from qawrench.cli import main

raise SystemExit(main(["flaky", *sys.argv[1:]]))
