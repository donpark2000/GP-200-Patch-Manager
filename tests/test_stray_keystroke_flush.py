"""Real-hardware testing (2026-09-28) found the no-args "press Enter to
continue" pause could be satisfied instantly on a genuine double-click --
the window still vanished before the message could be read. The likely
cause: dismissing the "Windows protected your PC" SmartScreen dialog via
the Enter key (its default action) can leave that keystroke sitting in the
brand-new console's input buffer, which input() then reads immediately
instead of waiting for the user's own keypress.

The fix: flush the console's input buffer (_flush_stray_windows_keystrokes,
via the Windows FlushConsoleInputBuffer API) right before prompting, so any
leftover keystroke is discarded first. Covers that this actually happens
before input() (not after, where it'd be useless), and that a failure in
the flush itself (unavailable API, anything) doesn't accidentally skip the
pause altogether -- a missed flush is a minor risk of the old bug
recurring, but silently skipping the pause would bring back the original,
worse bug (message unreadable at all) for everyone, not just an unlucky few.
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


def run_paused_case(flush_raises: bool):
    """Runs main() with no args on a simulated frozen build (the one case
    that pauses), recording the order flush/input were called in."""
    orig_argv = sys.argv
    orig_frozen = getattr(sys, "frozen", None)
    had_frozen_attr = hasattr(sys, "frozen")
    orig_flush_fn = gp200._flush_stray_windows_keystrokes

    call_order = []

    def fake_flush():
        call_order.append("flush")
        if flush_raises:
            raise OSError("simulated: FlushConsoleInputBuffer unavailable")

    def fake_input(prompt=""):
        call_order.append("input")
        return ""

    sys.argv = ["gp200.exe"]
    sys.frozen = True
    gp200._flush_stray_windows_keystrokes = fake_flush

    import builtins
    orig_builtin_input = builtins.input
    builtins.input = fake_input
    buf = io.StringIO()
    exit_code = None
    try:
        with contextlib.redirect_stdout(buf):
            try:
                gp200.main()
            except SystemExit as e:
                exit_code = e.code
    finally:
        builtins.input = orig_builtin_input
        sys.argv = orig_argv
        gp200._flush_stray_windows_keystrokes = orig_flush_fn
        if had_frozen_attr:
            sys.frozen = orig_frozen
        elif hasattr(sys, "frozen"):
            del sys.frozen

    return call_order, exit_code


# --- normal case: flush succeeds, and happens BEFORE input() -- flushing
#     after the prompt is already showing would be too late to matter. ---
order, code = run_paused_case(flush_raises=False)
check("flush is actually called in the pause path",
      "flush" in order)
check("input() is still called (the pause itself still happens)",
      "input" in order)
check("flush happens BEFORE input() -- discarding stray keystrokes only "
      "helps if it happens before the prompt reads them",
      order == ["flush", "input"])
check("exits cleanly",
      code == 0)

# --- the flush call fails for some reason (API unavailable, etc.): the
#     pause must still happen -- a missed flush just risks the original
#     leaked-keystroke bug recurring for this one run, but SKIPPING the
#     pause entirely would silently bring back the much worse bug (message
#     never readable at all) for every run. ---
order2, code2 = run_paused_case(flush_raises=True)
check("flush was attempted even though it's going to fail",
      "flush" in order2)
check("input() STILL runs even after the flush raised -- fail-safe, not "
      "fail-skip-the-pause",
      "input" in order2)
check("exits cleanly even after the flush error",
      code2 == 0)

# --- the flush itself must be a no-op (never touch the Windows-only API)
#     on a non-Windows frozen build (gp200-linux / gp200-macos), which have
#     no SmartScreen-dismissal path to begin with. ---
orig_platform = sys.platform
sys.platform = "linux"
try:
    gp200._flush_stray_windows_keystrokes()  # must not raise
    flush_didnt_raise = True
except Exception:
    flush_didnt_raise = False
finally:
    sys.platform = orig_platform
check("the flush is a safe no-op on non-Windows builds (never touches "
      "ctypes.windll, which doesn't exist there)",
      flush_didnt_raise)

print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("ALL STRAY-KEYSTROKE-FLUSH CHECKS PASSED")
