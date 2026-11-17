import sys

from qawrench.cli import main

raise SystemExit(main(["statewatch", *sys.argv[1:]]))
