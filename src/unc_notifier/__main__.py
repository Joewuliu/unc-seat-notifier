"""Enables `python -m unc_notifier <command>` as an alternative to the installed
`unc-notifier` console script."""

import sys

from unc_notifier.cli import main

if __name__ == "__main__":
    sys.exit(main())
