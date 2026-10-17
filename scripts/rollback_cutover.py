"""One-command rollback of the cutover: `python scripts/rollback_cutover.py [--manifest <path>]`.

See scripts/cutover.py for what it restores.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cutover import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["rollback", *sys.argv[1:]]))
