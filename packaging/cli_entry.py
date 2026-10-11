"""Entry point of the console-only build (no Qt): ``nettransfer-cli serve|push|batch|test|hash``."""

import sys

from app.cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
