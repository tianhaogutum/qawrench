import sys

from qawrench.cli import main

raise SystemExit(main(["msw-contract", *sys.argv[1:]]))
