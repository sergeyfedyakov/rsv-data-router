"""python -m rsv_data_router — same as the rsv-data-router console script
(serve by default; set-secret / remove-secret subcommands)."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
