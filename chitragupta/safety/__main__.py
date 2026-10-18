"""`python -m chitragupta safety internal <profile> | session | selftest` — see safety/hook.py."""
import sys

from .hook import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
