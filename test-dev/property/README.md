# Module property tests

This directory contains the executable property suite requested for parsing
and replay robustness:

- `generate_corpus.py` deterministically creates 200 malformed modules. The
  corruption classes alternate `truncate`, `bad_pattern`, `huge_counts`, and
  `cycle_refs`; each record has the mutated byte offset in `manifest.jsonl`.
- `property_runner.c` loads and replays one malformed input. It is run in a
  fresh ASan/UBSan/LSan process per case, so a failure is attributable to one
  input and its mutation offset.
- `ref_compare.c` compares one valid module with an OpenMPT-generated trace.
  It checks every active sample/channel on every replay frame and prints the
  data line (`offset`) plus field and values on mismatch.
- `run_property.py` builds an instrumented static libxmp, generates the
  corpus, runs all 200 cases, then compares 50 valid OpenMPT corpus files.
  It also checks basic module/frame structural boundaries in addition to
  ASan's memory checks.

Run from the repository root:

```sh
python3 test-dev/property/run_property.py
```

Artifacts are written to `artifacts/property/`:

- `corpus/` and `corpus/manifest.jsonl`
- `malformed_results.jsonl`
- `malformed_summary.json`
- `reference_results.jsonl`
- `reference_summary.json`
- `SUMMARY.md`

The malformed runner enables leak detection with
`ASAN_OPTIONS=detect_leaks=1`. Any ASan report is classified as
`out_of_bounds`, any LSan report as `leak`, and any UBSan diagnostic as
`undefined_behavior`. Timeouts are failures as well.
