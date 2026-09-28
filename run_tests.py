#!/usr/bin/env python3
"""
run_tests.py -- the standard, repeatable regression suite for gp200.py.

Run this before AND after any change:

    py -3.12 run_tests.py

Every file in tests/test_*.py is a self-contained, hardware-free check
(fake/mock devices only -- nothing here needs a GP-200 plugged in, and
nothing here writes anywhere except its own private temp directory). Each
one already prints its own PASS/FAIL lines and exits 0 (all good) or
non-zero (something's wrong); this runner just finds them all, runs each
in its own subprocess (so one test's bug or crash can't take another test
down with it, or leak state into it), and gives one final verdict.

Exit code is 0 only if every test file passed -- safe to script around
later (a pre-commit hook, CI, whatever) if that's ever wanted, but the
point today is simpler: one command, run often, that re-checks every core
feature this tool has, every time something changes.

Add a new test: drop a test_whatever.py file into tests/ that imports
gp200.py the same way the existing ones do (see any existing test_*.py for
the pattern -- load it via importlib from ../gp200.py relative to the test
file's own location, never a hardcoded path) and this runner picks it up
automatically next run. No registration step.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent / "tests"

# Each test file dynamically loads gp200.py via importlib, which -- same as
# any normal Python import -- writes a compiled .pyc into a __pycache__
# folder next to it by default. Harmless, but this suite runs from wherever
# the project lives, not a scratch folder, so it shouldn't leave anything
# behind that wasn't there before a run. Suppressed centrally here rather
# than in every test file.
_CHILD_ENV = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}


def main():
    test_files = sorted(TESTS_DIR.glob("test_*.py"))
    if not test_files:
        sys.exit(f"No test_*.py files found in {TESTS_DIR}")

    print(f"Running {len(test_files)} regression test file(s) from {TESTS_DIR}...\n")

    results = []
    for f in test_files:
        start = time.monotonic()
        proc = subprocess.run([sys.executable, str(f)], capture_output=True, text=True, env=_CHILD_ENV)
        elapsed = time.monotonic() - start
        ok = proc.returncode == 0
        results.append((f.name, ok))
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {f.name} ({elapsed:.1f}s)")
        if not ok:
            # Show enough of the output to diagnose without re-running by
            # hand -- the failing check(s) plus whatever crashed, if it did.
            combined = (proc.stdout + proc.stderr).strip().splitlines()
            tail = combined[-20:] if len(combined) > 20 else combined
            for line in tail:
                print(f"       {line}")
            print()

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\n{passed}/{total} test file(s) passed.")
    if passed != total:
        print("\nFailing:")
        for name, ok in results:
            if not ok:
                print(f"  - {name}")
        sys.exit(1)
    print("ALL REGRESSION TESTS PASSED")


if __name__ == "__main__":
    main()
