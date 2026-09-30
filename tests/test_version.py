"""Covers get_version()'s three-tier fallback (CI build stamp -> live git
log -> static SCRIPT_VERSION) and the --version CLI flag, added alongside
the flag itself (2026-09-30) so a hand-maintained SCRIPT_VERSION going
stale -- which it already had, in practice, see get_version()'s own
docstring -- can never again be the only way to identify an exact build."""
import contextlib, importlib.util, io, subprocess, sys, tempfile, types
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


class FakeCompletedProcess:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


# ---------------------------------------------------------------------------
# Tier 1: a _build_version module (as CI generates before PyInstaller
# freezes the script) takes priority over everything else, including a
# perfectly good live git checkout.
# ---------------------------------------------------------------------------
fake_build_version = types.ModuleType("_build_version")
fake_build_version.BUILD_VERSION = "f4a586e 2026-09-30"
sys.modules["_build_version"] = fake_build_version
try:
    check("get_version(): a _build_version module (CI/frozen-exe stamp) wins outright",
          gp200.get_version() == "f4a586e 2026-09-30")
finally:
    del sys.modules["_build_version"]

# ---------------------------------------------------------------------------
# Tier 2: no build stamp, but `git log` succeeds -- the common case for
# running gp200.py directly from a clone.
# ---------------------------------------------------------------------------
orig_run = gp200.subprocess.run
gp200.subprocess.run = lambda *a, **kw: FakeCompletedProcess(0, "abc1234 2026-09-30\n")
try:
    check("get_version(): falls back to live `git log` output when no build stamp exists",
          gp200.get_version() == "abc1234 2026-09-30")
finally:
    gp200.subprocess.run = orig_run

# ---------------------------------------------------------------------------
# Tier 3a: git present but this isn't a repo (non-zero exit, no stdout) --
# falls all the way back to the static SCRIPT_VERSION, clearly flagged as
# approximate rather than silently presented as exact.
# ---------------------------------------------------------------------------
gp200.subprocess.run = lambda *a, **kw: FakeCompletedProcess(128, "")
try:
    v = gp200.get_version()
    check("get_version(): a non-zero git exit falls back to SCRIPT_VERSION",
          gp200.SCRIPT_VERSION in v)
    check("get_version(): the SCRIPT_VERSION fallback is clearly flagged as approximate, "
          "not presented as if it were an exact revision",
          "unknown" in v.lower())
finally:
    gp200.subprocess.run = orig_run

# ---------------------------------------------------------------------------
# Tier 3b: git isn't even installed (subprocess raises) -- same fallback,
# must not crash.
# ---------------------------------------------------------------------------
def raise_not_found(*a, **kw):
    raise FileNotFoundError("git not found")
gp200.subprocess.run = raise_not_found
try:
    v = gp200.get_version()
    check("get_version(): git not being installed at all doesn't crash, "
          "falls back to SCRIPT_VERSION",
          gp200.SCRIPT_VERSION in v)
finally:
    gp200.subprocess.run = orig_run

# ---------------------------------------------------------------------------
# --version: prints something and exits 0, without needing a subcommand or
# a device connection at all -- argparse's built-in version action handles
# this before the "a subcommand is required" check ever fires.
# ---------------------------------------------------------------------------
orig_get_version = gp200.get_version
gp200.get_version = lambda: "deadbeef 2026-09-30"
orig_argv = sys.argv
sys.argv = ["gp200.py", "--version"]
buf = io.StringIO()
exit_code = None
try:
    with contextlib.redirect_stdout(buf):
        try:
            gp200.main()
        except SystemExit as e:
            exit_code = e.code
finally:
    sys.argv = orig_argv
    gp200.get_version = orig_get_version

out = buf.getvalue()
check("--version: exits cleanly (code 0)", exit_code == 0)
check("--version: prints the program name", "gp200.py" in out)
check("--version: prints get_version()'s value, not a hardcoded string",
      "deadbeef 2026-09-30" in out)
check("--version: doesn't require picking a subcommand first",
      "invalid choice" not in out.lower() and "the following arguments are required" not in out.lower())

# ---------------------------------------------------------------------------
# End-to-end, real subprocess (same reasoning as test_log_file.py: this is
# about what --log-file actually writes to disk from a fresh interpreter,
# not about mocking get_version() out of the picture): the log header's
# version field is non-empty and get_version() didn't raise partway through
# writing it.
# ---------------------------------------------------------------------------
GP200_PATH_STR = GP200_PATH
PY = sys.executable
with tempfile.TemporaryDirectory() as tmp:
    log_path = Path(tmp) / "version_check.log"
    subprocess.run([PY, GP200_PATH_STR, "--log-file", str(log_path), "read", "not-a-slot"],
                   capture_output=True, text=True)
    log_text = log_path.read_text() if log_path.exists() else ""
    check("--log-file header (real subprocess): has a non-empty version field, "
          "proving get_version() ran and didn't raise",
          "gp200.py run log -- version " in log_text
          and not log_text.split("run log -- version ", 1)[1].startswith(", started"))

print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("ALL VERSION CHECKS PASSED")
