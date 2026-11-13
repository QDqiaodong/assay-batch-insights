# Serial acquisition of synthetic instrument runs

`assay_insights.acquire` is a standalone, standard-library-only CLI that
captures one run of timed measurements from a synthetic instrument over a
POSIX serial line (8N1), validates the staged run with the existing
`assay_insights.input.load` contract, and atomically publishes per-run
artifacts. It never analyzes: the deliverables are `results.csv`, the raw
frames and a deterministic summary. The original `load`/`analyze`/`run`
functions, the `python -m assay_insights` CLI and the warehouse CLI are
unchanged.

```sh
python -m assay_insights.acquire \
  --device /dev/ttyUSB0 --baud 115200 \
  --calibrations data/timed_calibrations.csv \
  --policy data/policy.json \
  --output run/acquire --timeout 10
```

* `--device` (required): serial device path; opened `O_RDWR | O_NOCTTY` and
  configured raw 8N1 (8 data bits, no parity, one stop bit, no flow control).
* `--baud`: one of the platform `termios` rates (default 115200).
* `--calibrations`: must be a *timed* calibration CSV; the run is validated
  against this exact file.
* `--policy`: policy JSON consumed by `load` at validation time.
* `--output`: run directories publish under `<output>/<run_id>`; staging
  lives under `<output>/.staging/`.
* `--timeout`: finite idle-read timeout in seconds; must be a positive,
  finite number. Every read waits at most this long, so the CLI never
  blocks forever on a silent device. EOF (device hangup) fails the run
  immediately.

Exit codes: `0` on success (including an idempotent replay), `2` on any
failure. The deterministic summary is printed to stdout as one JSON object;
progress and diagnostics go to stderr.

## 1. Wire protocol

### Frames

```text
@ <8 hex length> : <UTF-8 JSON payload> * <8 hex CRC32> LF
```

* `length`: payload byte count as 8 hex digits. Payloads are at most
  65536 bytes; a larger declared length is rejected immediately, so the
  receive buffer stays bounded by one maximum frame (65556 bytes).
* `CRC32`: `zlib.crc32` of the payload bytes as 8 hex digits.
* The receiver is a streaming parser: frames may arrive split across many
  reads or stuck together in one read.

### Messages

Strict JSON objects: exact key sets, no duplicate keys, no `NaN`/`Infinity`,
booleans are not integers. `seq` is a continuous counter.

* `begin` — seq 0:
  `{"type":"begin","seq":0,"run_id":...,"batch":...,"count":N,"calibration_sha256":...}`
  * `run_id`: 1–128 chars of `[A-Za-z0-9._-]`, starting alphanumeric. It
    becomes the output directory name, so separators and dot-names are
    rejected.
  * `batch`: 1–128 printable characters, no control characters.
  * `count`: positive integer number of measurement records.
  * `calibration_sha256`: 64 lowercase hex digits; must equal the SHA-256
    of the `--calibrations` file.
* `measurement` — seq 1..count: the nine timed result CSV fields
  (`batch,sample,analyte,replicate,kind,signal,dilution,target_mg_l,measured_at`),
  all JSON strings, plus `seq`. `batch` must equal the begin batch.
* `end` — seq count+1:
  `{"type":"end","seq":N+1,"count":N,"measurements_sha256":...}` where the
  digest is the SHA-256 over each accepted measurement payload with an LF
  appended, in seq order.

### Replies

Single ASCII lines: `ACK <seq>` or `REJ <seq> <code>` with
`code` in `crc, frame, sequence, state, conflict, validation, internal`.
`REJ` carries the sequence number the receiver is waiting for. Except for
the single CRC retry, a `REJ` ends the session with a non-zero exit.

### Stop-and-wait with persistence

Every accepted frame is appended to `frames.log` (and its row to
`results.csv`) and both files are fsynced *before* the `ACK` is sent.

* A byte-exact retransmission of the immediately previous frame is only
  re-ACKed — never rewritten.
* A retransmission with different bytes, or any missing/out-of-order
  sequence, is rejected.
* A CRC failure on the expected sequence is `REJ crc` and retried exactly
  once; a second consecutive failure aborts the run.

## 2. Validation and atomic publish

After `end` verifies (count and measurements digest), the receiver:

1. writes `summary.json` into the staging directory and fsyncs it;
2. validates the staged run with the existing
   `load(calibrations, staging/results.csv, policy)` contract — the same
   header, type, range, timestamp and calibration-coverage rules as the
   offline CLI;
3. publishes atomically: `rename(<output>/.staging/<run_id>, <output>/<run_id>)`
   — same filesystem, so the run directory appears in one step — then
   fsyncs the output directory;
4. only then sends `ACK` for the `end` frame.

Any failure keeps the unpublished staging directory plus an `error.json`
diagnostic and exits non-zero; the old published directory, if any, is
never touched. Output paths are checked so they can never overwrite the
calibration or policy inputs.

## 3. Restart and replay

* One CLI invocation serves exactly one run. If the instrument or the CLI
  dies mid-run, the next invocation starts from `begin` again; a leftover
  staging directory is archived to `.staging/<run_id>.retired-<n>` before
  the replay starts.
* Replaying an already published run still ACKs every frame. The `end`
  frame is idempotent only when the run declares the same data, calibration
  and policy as the published summary (run_id, batch, count,
  `calibration_sha256`, `policy_sha256` and `measurements_sha256`); the old
  directory is kept as-is. Anything else is rejected and the old directory
  is kept.
* The device and the output directory are held under exclusive
  non-blocking `flock` locks for the whole session, so two acquisitions
  cannot interleave on one device or one output tree.

## 4. Deliverables of one run

`<output>/<run_id>/` contains exactly:

* `results.csv` — the timed results CSV (LF line endings), accepted by
  `load` and therefore by the existing analysis CLI;
* `frames.log` — the accepted wire frames, byte-exact, in order;
* `summary.json` — the deterministic summary:

```json
{
  "format": "assay-insights.serial-run/1",
  "run_id": "...", "batch": "...", "count": 3,
  "calibration_sha256": "...", "policy_sha256": "...",
  "measurements_sha256": "...",
  "results_sha256": "...", "frames_sha256": "..."
}
```

The summary contains no timestamps or absolute paths: identical runs
produce identical bytes, and every digest can be recomputed independently
from the delivered files and the inputs.

## 5. Pseudo-terminal tester

`assay_insights.acquire.tester` is the synthetic instrument used by the
test-suite, and a runnable end-to-end demo:

```sh
python -m assay_insights.acquire.tester --output run/acquire-demo
```

It allocates a pty pair, spawns the acquisition CLI on the slave side and
plays one full stop-and-wait run on the master side, printing a JSON
result. As a library it offers `open_pty`, the `Instrument` class
(stop-and-wait sessions, split/sticky writes, bad-CRC injection, reply
parsing) and the canonical payload builders shared with the receiver.
