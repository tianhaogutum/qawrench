import sys

from qawrench.cli import main

raise SystemExit(main(["frontier", *sys.argv[1:]]))
