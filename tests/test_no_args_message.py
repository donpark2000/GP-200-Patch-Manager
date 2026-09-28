"""Someone downloading the standalone executable is likely to just
double-click it the way any other Windows program is launched, not open a
terminal first. A console app with no arguments and argparse's default
`required=True` subparser error exits immediately with a technical usage
message -- for a double-clicked .exe that means a window flashes open and
closes before anyone can read it (2026-09-28, ahead of a planned public
share of the Windows build in a non-technical Facebook group).

Covers: the friendly zero-args message appears (instead of argparse's
error), it's plain-language rather than the dense protocol-notes epilog,
and it pauses for a keypress on any frozen (PyInstaller) build, never for a
source-code `python gp200.py` run.

This intentionally does NOT try to tell "double-clicked, fresh console"
apart from "frozen exe run from an already-open terminal" -- an earlier
version of this fix tried, via GetConsoleProcessList, specifically to avoid
pausing in the terminal case (where "press Enter to close this window"
doesn't make sense). Real-hardware testing the same day found that
detection unreliable (see _should_pause_before_exit's docstring for why),
so it was dropped in favor of always pausing when frozen, with the prompt
worded ("Press Enter to continue...") to make sense in either case instead.
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


def run_main_no_args(frozen: bool, prog: str = "gp200.py"):
    """Runs gp200.main() with no CLI args, as if launched with sys.argv =
    [prog], optionally simulating a PyInstaller-frozen executable. Returns
    (captured_stdout, exit_code, input_call_count)."""
    orig_argv = sys.argv
    orig_frozen = getattr(sys, "frozen", None)
    had_frozen_attr = hasattr(sys, "frozen")

    input_calls = []
    def fake_input(prompt=""):
        input_calls.append(prompt)
        return ""  # simulates a keypress

    sys.argv = [prog]
    sys.frozen = True if frozen else False
    if not frozen and not had_frozen_attr:
        del sys.frozen  # match "no sys.frozen attribute at all" exactly

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

# --- frozen executable, any invocation context: same message, but now it
#     MUST pause -- deliberately not trying to detect double-click vs an
#     existing terminal (see module docstring for why that was dropped). ---
out2, code2, input_calls2 = run_main_no_args(frozen=True, prog="gp200.exe")
check("no-args (frozen exe): exits cleanly after the keypress",
      code2 == 0)
check("no-args (frozen exe): shows the same friendly message",
      "terminal" in out2.lower() and "list-ports" in out2)
check("no-args (frozen exe): uses the exe's own name in the examples",
      "gp200.exe " in out2)
check("no-args (frozen exe): DOES pause for a keypress -- the fix for the "
      "window-closes-instantly problem",
      input_calls2 == 1)
check("no-args (frozen exe): the prompt is worded to make sense whether or not "
      "the window is actually about to close (not 'close this window')",
      "close this window" not in out2.lower())

print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("ALL NO-ARGS MESSAGE CHECKS PASSED")
