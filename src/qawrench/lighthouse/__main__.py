import sys

from qawrench.cli import main

raise SystemExit(main(["lighthouse", *sys.argv[1:]]))
