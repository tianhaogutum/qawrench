import sys

from qawrench.cli import main

raise SystemExit(main(["a11y", *sys.argv[1:]]))
