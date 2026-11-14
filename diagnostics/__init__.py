"""Ambient-state diagnostics for Assay Batch Insights.

This package is deliberately standalone: it imports the product only through
its public entry points, never edits it, and keeps every observation in a
per-trial isolated process.

Modules:

* :mod:`fixtures` - exact synthetic fixtures with independent hand-derived
  expectations;
* :mod:`common` - context description/application and stable serialisation;
* :mod:`trial` - one isolated process executing one scenario with one factor;
* :mod:`orchestrator` - frozen-input setup, trial matrix, comparison and report.

The command-line entry point is :mod:`diagnostics.cli`.
"""

__all__ = ["fixtures", "common", "trial", "orchestrator"]
