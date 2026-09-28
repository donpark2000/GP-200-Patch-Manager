"""--log-file is exercised as real subprocess runs, not mocks: its whole
point is capturing things (a sys.exit(...) message, an uncaught traceback)
that are printed further up the call stack than any function inside this
process, and an atexit-ordering bug (see PROTOCOL_NOTES.md) only shows up
at real interpreter shutdown. A same-process unit test would miss both."""
import subprocess
import sys
import tempfile
from pathlib import Path

GP200 = str(Path(__file__).resolve().parent.parent / "gp200.py")
PY = sys.executable

failures = []
def check(name, cond):
    print(("[PASS] " if cond else "[FAIL] ") + name)
    if not cond:
        failures.append(name)

with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)

    # 1. A command that fails cleanly via sys.exit(...) (bad slot label,
    #    validated before any device connection) -- the log should contain
    #    the header, the error message, and exit code should be 1 (not the
    #    120 the atexit-ordering bug produced before it was fixed).
    log1 = tmp / "run1.log"
    p1 = subprocess.run([PY, GP200, "--log-file", str(log1), "read", "not-a-slot"],
                         capture_output=True, text=True)
    check("sys.exit path: process exits 1, not the atexit-bug's 120", p1.returncode == 1)
    check("sys.exit path: log file was created", log1.exists())
    log1_text = log1.read_text() if log1.exists() else ""
    check("sys.exit path: log has the run header", "gp200.py run log" in log1_text)
    check("sys.exit path: log has the environment block", "python:" in log1_text and "OS:" in log1_text)
    check("sys.exit path: log captured the actual error message",
          "invalid slot label" in log1_text)
    check("sys.exit path: log has the run-ended footer", "run ended" in log1_text)
    check("sys.exit path: console output ALSO shows the error (Tee, not log-only)",
          "invalid slot label" in p1.stdout + p1.stderr)

    # 2. --log-file with no value at all must fail loudly (argparse's own
    #    error), never silently swallow the next token (the historical bug:
    #    nargs='?' ate a subcommand name as the log path instead).
    p2 = subprocess.run([PY, GP200, "--log-file"], capture_output=True, text=True)
    check("--log-file alone: argparse refuses it (exit 2)", p2.returncode == 2)
    check("--log-file alone: argparse's own clear error, not a silent misparse",
          "expected one argument" in p2.stderr)

    # 3. The specific historical bug: --log-file directly followed by a
    #    subcommand name must run THAT subcommand with logging on, not
    #    treat the subcommand name as the log's filename.
    log3 = tmp / "run3.log"
    p3 = subprocess.run([PY, GP200, "--log-file", str(log3), "list-ports"],
                         capture_output=True, text=True)
    check("--log-file PATH list-ports: does not swallow the subcommand",
          "the following arguments are required: cmd" not in p3.stderr)
    check("--log-file PATH list-ports: the exact path given was used, not the subcommand name",
          log3.exists() and not Path("list-ports").exists())

    # 4. A file left literally named after a subcommand must never be
    #    created by mistake (the exact symptom of the nargs='?' bug).
    check("no stray file named 'list-ports' was created anywhere", not Path("list-ports").exists())

print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("ALL LOG-FILE CHECKS PASSED")
