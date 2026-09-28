"""Someone downloading the standalone executable is likely to just
double-click it the way any other Windows program is launched, not open a
terminal first. A console app with no arguments and argparse's default
`required=True` subparser error exits immediately with a technical usage
message -- for a double-clicked .exe that means a window flashes open and
closes before anyone can read it (2026-09-28, ahead of a planned public
share of the Windows build in a non-technical Facebook group).

Covers: the friendly zero-args message appears (instead of argparse's
error), it's plain-language rather than the dense protocol-notes epilog,
and the window-closes-too-fast problem is fixed by waiting for a keypress --
but ONLY for the specific case that actually needs it: a frozen .exe running
in a console Windows just created for it (the double-click case). Real
testing the same day found the first version of this fix paused even when
the .exe was run from an ALREADY-OPEN command prompt, where "press Enter to
close this window" makes no sense (the window isn't closing). Covers all
three cases: source-code run, frozen exe in a fresh console, and frozen exe
run from an existing terminal.
"""
import contextlib, importlib.util, io, sys
from pathlib import Path

GP200_PATH = str(Path(__file__).resolve().parent.parent / "gp200.py")
spec = importlib.util.spec_from_file_location("gp200", GP200_PATH)
gp200 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gp200)

failures = []
def check(name, cond):
    print(("[PASS] " if cond else "[FAIL] ") + name)
    if not cond:
        failures.append(name)


def run_main_no_args(frozen: bool, prog: str = "gp200.py",
                      platform: str = "linux", console_process_count=None):
    """Runs gp200.main() with no CLI args, simulating: whether this is a
    frozen PyInstaller exe (sys.frozen), what OS it's "running on"
    (sys.platform), and -- only consulted on a simulated Windows run --
    how many processes GetConsoleProcessList would report (1 = a fresh
    console Windows just created for a double-click; >1 = an already-open
    shell this attached to). Returns (captured_stdout, exit_code,
    input_call_count)."""
    orig_argv = sys.argv
    orig_frozen = getattr(sys, "frozen", None)
    had_frozen_attr = hasattr(sys, "frozen")
    orig_platform = sys.platform
    orig_console_count_fn = gp200._windows_console_process_count

    input_calls = []
    def fake_input(prompt=""):
        input_calls.append(prompt)
        return ""  # simulates a keypress

    def fake_console_count():
        if console_process_count is None:
            raise AssertionError("test bug: GetConsoleProcessList called but "
                                  "console_process_count wasn't given")
        return console_process_count

    sys.argv = [prog]
    sys.frozen = True if frozen else False
    if not frozen and not had_frozen_attr:
        del sys.frozen  # match "no sys.frozen attribute at all" exactly, not just False
    sys.platform = platform
    gp200._windows_console_process_count = fake_console_count

    buf = io.StringIO()
    exit_code = None
    # main() uses the builtin input() directly (not a gp200-level name), so
    # patch it at the builtins level for the duration of this call only.
    import builtins
    orig_builtin_input = builtins.input
    builtins.input = fake_input
    try:
        with contextlib.redirect_stdout(buf):
            try:
                gp200.main()
            except SystemExit as e:
                exit_code = e.code
    finally:
        builtins.input = orig_builtin_input
        sys.argv = orig_argv
        sys.platform = orig_platform
        gp200._windows_console_process_count = orig_console_count_fn
        if had_frozen_attr:
            sys.frozen = orig_frozen
        elif hasattr(sys, "frozen"):
            del sys.frozen

    return buf.getvalue(), exit_code, len(input_calls)


# --- source-code style invocation (python gp200.py, no args): friendly
#     message, clean exit, and NO pause -- the terminal was already open. ---
out, code, input_calls = run_main_no_args(frozen=False, prog="gp200.py")
check("no-args (source): exits cleanly (code 0), not argparse's usage error",
      code == 0)
check("no-args (source): explains this is a terminal program, not a double-click app",
      "terminal" in out.lower())
check("no-args (source): shows at least one concrete example command",
      "list-ports" in out and "export --all" in out)
check("no-args (source): points to --help for the rest",
      "--help" in out)
check("no-args (source): uses the ACTUAL program name (gp200.py), not a hardcoded guess",
      "gp200.py " in out or "gp200.py\n" in out)
check("no-args (source): does NOT pause for a keypress (terminal isn't going anywhere)",
      input_calls == 0)
check("no-args (source): does not dump the dense protocol/status notes at someone "
      "who hasn't even picked a command yet",
      "PROTOCOL CREDIT" not in out and "RigSheet" not in out)

# --- frozen exe, freshly-created console (the actual double-click case):
#     GetConsoleProcessList would report just this one process attached.
#     This is the ONE case that must pause. ---
out2, code2, input_calls2 = run_main_no_args(
    frozen=True, prog="gp200.exe", platform="win32", console_process_count=1)
check("no-args (frozen exe, fresh console): exits cleanly after the keypress",
      code2 == 0)
check("no-args (frozen exe, fresh console): shows the same friendly message",
      "terminal" in out2.lower() and "list-ports" in out2)
check("no-args (frozen exe, fresh console): uses the exe's own name in the examples",
      "gp200.exe " in out2)
check("no-args (frozen exe, fresh console): DOES pause for a keypress -- the actual "
      "fix for the window-closes-instantly problem",
      input_calls2 == 1)

# --- frozen exe, run from an ALREADY-OPEN command prompt: this is the bug
#     found by hand on 2026-09-28 -- GetConsoleProcessList reports more than
#     one process (the shell plus this one), meaning the console isn't
#     closing when this process exits, so pausing makes no sense here. ---
out3, code3, input_calls3 = run_main_no_args(
    frozen=True, prog="gp200.exe", platform="win32", console_process_count=2)
check("no-args (frozen exe, existing terminal): exits cleanly, no keypress needed",
      code3 == 0)
check("no-args (frozen exe, existing terminal): shows the same friendly message",
      "terminal" in out3.lower() and "list-ports" in out3)
check("no-args (frozen exe, existing terminal): does NOT pause -- the window it's "
      "running in isn't going anywhere, so 'press Enter to close' would be confusing",
      input_calls3 == 0)

# --- frozen exe, but NOT actually on Windows (a Linux/macOS PyInstaller
#     build): GetConsoleProcessList doesn't exist there at all, so this must
#     never even try to call it, and never pauses. ---
out4, code4, input_calls4 = run_main_no_args(
    frozen=True, prog="gp200-linux", platform="linux")  # no console_process_count given
check("no-args (frozen exe, non-Windows): exits cleanly",
      code4 == 0)
check("no-args (frozen exe, non-Windows): never calls the Windows-only console check "
      "(would raise in this test if it tried) and never pauses",
      input_calls4 == 0)

print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("ALL NO-ARGS MESSAGE CHECKS PASSED")
