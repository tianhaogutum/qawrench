import sys

from qawrench.cli import main

raise SystemExit(main(["crawlee-export", *sys.argv[1:]]))
