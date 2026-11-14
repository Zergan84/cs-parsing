# Archived tests

Run an archived test file explicitly, for example:

```sh
python -m pytest archive/legacy_tests/test_unit.py
python -m pytest archive/legacy_tests/test_phase0_infrastructure.py
```

The project-level `pytest.ini` limits ordinary discovery to `tests/`; passing
an archive path explicitly still collects these tests. The conftest here only
adds `archive/legacy_scripts` to imports needed by old phase-zero tests.

These files are historical checks, not the current product gate. Tests for the
legacy queue, `hosts`/`trip_hosts`, JSON queue files, old profile analyzer, and
old UI endpoints document retired behavior. Interactive browser and model tests
(`test_collect_photos.py`, `test_gender.py`, `test_llava_*.py`, `test_llm.py`,
`test_save_to_db.py`) depend on local credentials, external services, or a live
browser. `test_unit.py` and parts of `test_cleaner.py` retain useful pure-helper
regressions; current equivalents belong in `tests/` before they are removed.

The archive suite is intentionally not silently rewritten to match the current
architecture. Some files no longer collect because their required compatibility
symbols were removed; others collect but fail assertions for retired behavior.
For example, `test_auto_sender_candidates.py` imports the removed `QUEUE_FILE`,
and selected tests in `test_unit.py` expect trip context and QC APIs that no
longer exist in those legacy modules. `test_unit.py` and `test_cleaner.py` were
exercised during this audit: 25 passed, 7 failed due to those incompatibilities.
Failures there are historical incompatibility or candidate coverage to migrate,
separate from failures under `tests/`.
