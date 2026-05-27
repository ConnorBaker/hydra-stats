"""Enable ``python -m hydra_stats``."""

import sys

from hydra_stats.cli import main

if __name__ == "__main__":
    sys.exit(main())
