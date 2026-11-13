"""Wire protocol for the synthetic instrument serial acquisition channel.

Frame layout (envelope bytes are ASCII)::

    @ <8 hex length> : <UTF-8 JSON payload> * <8 hex CRC32> LF

* length: payload byte count as 8 hex digits; payload is at most 65536 bytes.
* CRC32: ``zlib.crc32`` of the payload bytes as 8 hex digits.

Messages are strict JSON objects with an exact key set per type:

* ``begin`` (seq 0): ``run_id``, ``batch``, positive ``count`` and the
  ``calibration_sha256`` of the timed calibration CSV the run relies on.
* ``measurement`` (seq 1..count): the nine timed result CSV fields, all
  strings, plus the continuous ``seq``.
* ``end`` (seq count+1): ``count`` and ``measurements_sha256``, the SHA-256
  over each accepted measurement payload with an LF appended, in seq order.

Replies are single ASCII lines: ``ACK <seq>`` or ``REJ <seq> <code>``.
"""
import hashlib
import json
import re
import zlib

from ..input import TIMED_RESULT_HEADERS
from .errors import RejectError

MAX_PAYLOAD = 65536
MAX_FRAME = 1 + 8 + 1 + MAX_PAYLOAD + 1 + 8 + 1
MAX_COUNT = 2**31 - 1

RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
HEX64_RE = re.compile(r"[0-9a-f]{64}\Z")
_HEX_DIGITS = frozenset(b"0123456789abcdefABCDEF")

BEGIN = "begin"
MEASUREMENT = "measurement"
END = "end"

BEGIN_KEYS = frozenset({"type", "seq", "run_id", "batch", "count", "calibration_sha256"})
MEASUREMENT_KEYS = frozenset({"type", "seq", *TIMED_RESULT_HEADERS})
END_KEYS = frozenset({"type", "seq", "count", "measurements_sha256"})

SUMMARY_FORMAT = "assay-insights.serial-run/1"


class Frame:
    """One parsed frame: exact wire bytes, payload and CRC verdict."""

    __slots__ = ("raw", "payload", "crc_ok")

    def __init__(self, raw, payload, crc_ok):
        self.raw = raw
        self.payload = payload
        self.crc_ok = crc_ok


def crc32_hex(payload):
    return f"{zlib.crc32(payload) & 0xFFFFFFFF:08x}"


def encode_frame(payload):
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"payload exceeds {MAX_PAYLOAD} bytes")
    return b"@" + f"{len(payload):08x}".encode("ascii") + b":" + payload + b"*" + crc32_hex(payload).encode("ascii") + b"\n"


def _is_hex8(field):
    return len(field) == 8 and all(byte in _HEX_DIGITS for byte in field)


def try_parse_frame(buffer):
    """Parse one frame out of ``buffer`` (a bytearray).

    Returns a Frame and consumes its bytes, or None when more bytes are
    needed. Structural envelope errors raise RejectError("frame", ...);
    a bad CRC does not: it yields a Frame with ``crc_ok`` False so the
    receiver can apply its single-retry rule.
    """
    size = len(buffer)
    if size == 0:
        return None
    if buffer[0] != 0x40:  # '@'
        raise RejectError("frame", "frame must start with '@'")
    if size < 10:
        return None
    length_field = bytes(buffer[1:9])
    if not _is_hex8(length_field):
        raise RejectError("frame", "length field must be 8 hex digits")
    if buffer[9] != 0x3A:  # ':'
        raise RejectError("frame", "expected ':' after the length field")
    length = int(length_field, 16)
    if length > MAX_PAYLOAD:
        raise RejectError("frame", f"payload length {length} exceeds {MAX_PAYLOAD}")
    total = 20 + length
    if size < total:
        return None
    if buffer[10 + length] != 0x2A:  # '*'
        raise RejectError("frame", "expected '*' after the payload")
    crc_field = bytes(buffer[11 + length:19 + length])
    if not _is_hex8(crc_field):
        raise RejectError("frame", "CRC field must be 8 hex digits")
    if buffer[19 + length] != 0x0A:  # LF
        raise RejectError("frame", "frame must end with LF")
    raw = bytes(buffer[:total])
    payload = bytes(buffer[10:10 + length])
    del buffer[:total]
    crc_ok = zlib.crc32(payload) & 0xFFFFFFFF == int(crc_field, 16)
    return Frame(raw, payload, crc_ok)


def _unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"duplicate key {key!r}")
        obj[key] = value
    return obj


def _reject_constant(value):
    raise ValueError(f"invalid JSON constant {value}")


def parse_message(payload):
    """Decode and strictly validate one message payload."""
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RejectError("frame", f"payload is not valid UTF-8: {exc}") from exc
    try:
        message = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise RejectError("frame", f"payload is not valid JSON: {exc}") from exc
    except ValueError as exc:
        raise RejectError("frame", f"payload JSON rejected: {exc}") from exc
    if not isinstance(message, dict):
        raise RejectError("frame", "message must be a JSON object")
    mtype = message.get("type")
    if mtype == BEGIN:
        _check_begin(message)
    elif mtype == MEASUREMENT:
        _check_measurement(message)
    elif mtype == END:
        _check_end(message)
    else:
        raise RejectError("frame", f"unknown message type {mtype!r}")
    return message


def _check_keys(message, keys, kind):
    if set(message) != keys:
        raise RejectError("frame", f"{kind} keys must be exactly {sorted(keys)}")


def _is_seq(value):
    return type(value) is int and value >= 0


def _is_count(value):
    return type(value) is int and 1 <= value <= MAX_COUNT


def _is_hex64(value):
    return isinstance(value, str) and HEX64_RE.fullmatch(value) is not None


def _check_begin(message):
    _check_keys(message, BEGIN_KEYS, "begin")
    if not _is_seq(message["seq"]):
        raise RejectError("frame", "begin seq must be a non-negative integer")
    run_id = message["run_id"]
    if not isinstance(run_id, str) or RUN_ID_RE.fullmatch(run_id) is None:
        raise RejectError("frame", "run_id must be 1-128 chars of [A-Za-z0-9._-] starting alphanumeric")
    batch = message["batch"]
    if (not isinstance(batch, str) or not batch or len(batch) > 128
            or any(ord(char) < 0x20 or ord(char) == 0x7F for char in batch)):
        raise RejectError("frame", "batch must be 1-128 printable characters without control characters")
    if not _is_count(message["count"]):
        raise RejectError("frame", f"count must be an integer between 1 and {MAX_COUNT}")
    if not _is_hex64(message["calibration_sha256"]):
        raise RejectError("frame", "calibration_sha256 must be 64 lowercase hex digits")


def _check_measurement(message):
    _check_keys(message, MEASUREMENT_KEYS, "measurement")
    if not _is_seq(message["seq"]) or message["seq"] == 0:
        raise RejectError("frame", "measurement seq must be a positive integer")
    for field in TIMED_RESULT_HEADERS:
        if not isinstance(message[field], str):
            raise RejectError("frame", f"measurement field {field!r} must be a string")


def _check_end(message):
    _check_keys(message, END_KEYS, "end")
    if not _is_seq(message["seq"]):
        raise RejectError("frame", "end seq must be a non-negative integer")
    if not _is_count(message["count"]):
        raise RejectError("frame", f"count must be an integer between 1 and {MAX_COUNT}")
    if not _is_hex64(message["measurements_sha256"]):
        raise RejectError("frame", "measurements_sha256 must be 64 lowercase hex digits")


def ack_line(seq):
    return f"ACK {seq}\n".encode("ascii")


def rej_line(seq, code):
    return f"REJ {seq} {code}\n".encode("ascii")


def parse_reply(line):
    """Parse an ACK/REJ line into (kind, seq, code|None). Raises ValueError."""
    parts = line.decode("ascii").split()
    if len(parts) == 2 and parts[0] == "ACK" and parts[1].isdigit():
        return ("ACK", int(parts[1]), None)
    if len(parts) == 3 and parts[0] == "REJ" and parts[1].isdigit():
        return ("REJ", int(parts[1]), parts[2])
    raise ValueError(f"malformed reply: {line!r}")


def canonical_json(obj):
    """Deterministic UTF-8 JSON used by the synthetic instrument."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def begin_payload(run_id, batch, count, calibration_sha256):
    return canonical_json({
        "type": BEGIN,
        "seq": 0,
        "run_id": run_id,
        "batch": batch,
        "count": count,
        "calibration_sha256": calibration_sha256,
    })


def measurement_payload(seq, fields):
    return canonical_json({
        "type": MEASUREMENT,
        "seq": seq,
        **{header: fields[header] for header in TIMED_RESULT_HEADERS},
    })


def end_payload(seq, count, measurements_sha256):
    return canonical_json({
        "type": END,
        "seq": seq,
        "count": count,
        "measurements_sha256": measurements_sha256,
    })


def measurements_digest(payloads):
    """SHA-256 over each measurement payload with an LF appended, in order."""
    hasher = hashlib.sha256()
    for payload in payloads:
        hasher.update(payload)
        hasher.update(b"\n")
    return hasher.hexdigest()
