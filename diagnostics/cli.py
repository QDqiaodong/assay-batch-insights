"""Command-line entry point for the ambient-context diagnostic."""
import sys

from .orchestrator import main

if __name__ == "__main__":
    sys.exit(main())
