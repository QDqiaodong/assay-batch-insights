"""Serial acquisition of synthetic instrument runs (stdlib + POSIX 8N1).

The package is independent of the report-generation and warehouse entry
points: it captures timed measurement frames from a serial device, validates
the staged run with the existing ``assay_insights.input.load`` contract, and
atomically publishes ``results.csv``, the raw frames and a deterministic
summary per run. It never analyzes.
"""
from .errors import AcquireError, RejectError
from .receiver import Config, serve

__all__ = [
    "AcquireError",
    "RejectError",
    "Config",
    "serve",
]
