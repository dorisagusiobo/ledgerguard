# Dev response recordings

`pipeline-dev.jsonl` must be captured from a real, explicitly budgeted dev run.
Do not substitute fake responses or record the held-out test split. The temporary
FakeClient round-trip tests exercise the recorder offline but are not a live
recording artifact and do not measure model quality.

With regenerated seed-7 synthetic data, a live capture is:

```sh
uv run python -m ledgerguard.pipeline_eval --data data/synthetic-v2 --split dev --live --record evals/recordings/pipeline-dev.jsonl --max-cost 0.50 --json runs/pipeline-recorded.json --audit runs/pipeline-recorded-audit.jsonl
```

Then replay without AWS credentials or SDK creation:

```sh
uv run python -m ledgerguard.pipeline_eval --data data/synthetic-v2 --split dev --replay evals/recordings/pipeline-dev.jsonl --max-cost 0.50 --json runs/pipeline-replayed.json --audit runs/pipeline-replayed-audit.jsonl
```

The gateway still accounts for recorded usage and enforces limits; replay incurs
no provider charges. JSON and terminal reports mark the result as `replayed`.
Prompts, request tool inputs and PDFs are not stored. Model output, stop reason
and usage are stored; private reasoning blocks are refused. Header provenance
includes seed, input hash, model IDs, git revision and UTC date.

The input fingerprint is SHA-256 of canonical JSON containing sorted pairs of
relative filenames and SHA-256 file hashes. It covers dev PDFs plus metadata,
split selection, contracts, history and policy; it does not read truth invoices,
answer keys or held-out PDFs before running. Request hashes use canonical JSON
of `{modelId, request}`, with binary document values replaced by SHA-256 digests.

Missing/exhausted keys, data/model changes and unused records fail fatally with a
re-record message. Every call is stored, including different responses to the same
request key. Replay keeps a queue per key and consumes responses in file order;
every queued response must be used. This is reproducible because the pipeline
processes invoices sequentially in a fixed order, without threads or async model
calls. Responses are preserved exactly, including protocol IDs and usage, without
normalization or substitution. A changed prompt/schema requires a new live
recording. Existing recording files are never overwritten.

`RecordingMismatch` deliberately derives from `BaseException`: it is a fatal
fixture-integrity error and must escape ordinary provider-error referral handling
at the governance boundary. The CLI catches it explicitly and exits nonzero.

Before committing a genuine recording, replay it and verify the fixed acceptance
checks in `tests/test_recording.py`: 14/14 findings, precision 100%, 0/20 clean
invoices flagged, exact extraction 38/38, notes for all 14 groups, zero referrals,
and accounted cost at most $0.50. Live evaluations before releases remain the
check for model changes; replay only detects regressions in our code.


Recording and replay always require the full dev split; `--limit` is refused.
Fatal interruptions during a client call append an `aborted` audit outcome and
retain invoice/batch reservations (and any monthly reservation) conservatively.

`test_committed_dev_recording_replays` reads `pipeline-dev.jsonl` directly and
applies the acceptance assertions above. Missing recordings skip locally but fail
with `CI=true`; no fake or live fallback is allowed. Commit the genuine recording
and this test together. Until then, CI intentionally fails on the missing file.
