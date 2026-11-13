"""Minimal POSIX serial configuration: 8N1, raw, no flow control.

Standard library only. ``VMIN=0, VTIME=0`` makes reads return with whatever
bytes are available; the receiver layers its finite select() timeout on top.
"""
import os
import termios


def _baud_rates():
    rates = {}
    for name, value in vars(termios).items():
        if name.startswith("B") and name[1:].isdigit():
            rates[int(name[1:])] = value
    return rates


BAUD_RATES = _baud_rates()


def configure(fd, baud):
    """Configure ``fd`` as raw 8N1 at ``baud`` (no parity, one stop bit)."""
    if baud not in BAUD_RATES:
        raise ValueError(f"unsupported baud rate {baud}; supported: {sorted(BAUD_RATES)}")
    attrs = termios.tcgetattr(fd)
    attrs[0] = 0  # iflag: no input processing, no CR/NL mapping, no IXON
    attrs[1] = 0  # oflag: no output processing
    # cflag: 8 data bits, no parity (PARENB clear), one stop bit (CSTOPB
    # clear), no hardware flow control; receiver on, ignore modem lines.
    attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
    attrs[3] = 0  # lflag: raw (no ICANON/ECHO/ISIG)
    attrs[4] = BAUD_RATES[baud]
    attrs[5] = BAUD_RATES[baud]
    attrs[6][termios.VMIN] = 0
    attrs[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, attrs)


def open_device(path, baud):
    """Open a serial device read/write without controlling-terminal side effects."""
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
    try:
        configure(fd, baud)
    except Exception:
        os.close(fd)
        raise
    return fd
