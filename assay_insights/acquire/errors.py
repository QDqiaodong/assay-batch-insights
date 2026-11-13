"""Failure types for the serial acquisition channel."""


class AcquireError(Exception):
    """Fatal acquisition failure; the CLI reports it and exits non-zero."""


class RejectError(AcquireError):
    """A peer frame is rejected; ``code`` is reported on the REJ line.

    The session keeps the unpublished staging directory and a diagnostic
    ``error.json`` whenever a run identity is known, then the CLI exits
    non-zero.
    """

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
