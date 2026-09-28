import argparse, importlib.util, io, contextlib, os, tempfile, atexit, shutil
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

# Scenarios 3, 4 and 6 below deliberately drive cmd_calibrate_settle to give
# up on an always-bad device, which hits the real, unmocked
# _save_failed_readback(slot, last_roundtrip) give-up path in gp200.py --
# this is the ACTUAL source of the failed_verify_1A_*.prst litter that kept
# appearing even after isolating test_soak.py the same way: this file had no
# isolation at all, and was never checked because its own [PASS] lines never
# mention "diagnostics" or "failed_verify". Found by grepping gp200.py for
# every _save_failed_readback call site and checking each test that reaches
# one, not by assuming test_soak.py was the only culprit.
_tmp = tempfile.mkdtemp(prefix="gp200_test_")
atexit.register(shutil.rmtree, _tmp, ignore_errors=True)
os.chdir(_tmp)

skeleton = gp200.resolve_skeleton_bytes(None)
file_bytes = skeleton
dump_clean = bytes(file_bytes)[gp200.CONTENT_FILE_START:gp200.CHECKSUM_OFF]
bad_bytes = bytearray(file_bytes)
bad_bytes[gp200.CONTENT_FILE_START + 100] ^= 0xFF
dump_bad = bytes(bad_bytes)[gp200.CONTENT_FILE_START:gp200.CHECKSUM_OFF]


def make_args(**overrides):
    ns = argparse.Namespace(
        slot="1-A", file="unused.prst", port=None, debug=False, force=True,
        commit=False, start_settle=1.0, step=0.1, target_streak=5,
        max_settle=5.0, max_attempts=200,
    )
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


class FakeDevBase:
    """Stands in for gp200.Device: records every write_slot's settle_s, and
    answers verify_write_full according to a subclass-supplied schedule."""
    def __init__(self):
        self.settle_values = []
        self.write_calls = 0
        self.closed = False

    def write_slot(self, slot, fb, currently_active, commit=False, settle_s=1.0):
        self.write_calls += 1
        self.settle_values.append(settle_s)

    def close(self):
        self.closed = True

    def read_name(self, slot):
        return "fake"


# --- Scenario 1: always clean -> reaches target streak at the starting delay,
#     never increments settle, never touches file I/O (Path.read_bytes is
#     bypassed by monkeypatching Path so the fake .prst name is accepted).
class FakeDevAlwaysClean(FakeDevBase):
    def verify_write_full(self, slot, fb, skel):
        roundtrip = gp200.build_prst_from_dump(dump_clean, "x", skel)
        mismatches = gp200.diff_prst_content(fb, roundtrip)
        return (len(mismatches) == 0), mismatches, gp200.prst_file_name(roundtrip), roundtrip


orig_device = gp200.Device
orig_read_bytes = gp200.Path.read_bytes
orig_resolve_verify_skeleton = gp200._resolve_verify_skeleton
orig_confirm_overwrite = gp200.confirm_overwrite

gp200.Path.read_bytes = lambda self: bytes(file_bytes)
gp200._resolve_verify_skeleton = lambda: skeleton
gp200.confirm_overwrite = lambda dev, slot, force: True

dev1 = FakeDevAlwaysClean()
gp200.Device = lambda *a, **kw: dev1
args1 = make_args()
out1 = io.StringIO()
with contextlib.redirect_stdout(out1):
    gp200.cmd_calibrate_settle(args1)
check("always-clean device: reaches target streak without any delay bump",
      dev1.write_calls == args1.target_streak and all(s == 1.0 for s in dev1.settle_values))
check("always-clean device: closes the connection", dev1.closed)
check("always-clean device: reports 0 mismatches in the summary", "0 mismatch(es)" in out1.getvalue())

# --- Scenario 2: fails at the starting delay, then succeeds once settle
#     crosses a threshold -- streak should reset on the failure and the
#     delay actually used should have been bumped by --step.
class FakeDevThreshold(FakeDevBase):
    def verify_write_full(self, slot, fb, skel):
        settle = self.settle_values[-1]
        dump = dump_clean if settle >= 1.25 else dump_bad
        roundtrip = gp200.build_prst_from_dump(dump, "x", skel)
        mismatches = gp200.diff_prst_content(fb, roundtrip)
        return (len(mismatches) == 0), mismatches, gp200.prst_file_name(roundtrip), roundtrip

dev2 = FakeDevThreshold()
gp200.Device = lambda *a, **kw: dev2
args2 = make_args(target_streak=3)
out2 = io.StringIO()
with contextlib.redirect_stdout(out2):
    gp200.cmd_calibrate_settle(args2)
check("threshold device: settle delay was bumped past the threshold (1.25)",
      max(dev2.settle_values) >= 1.25)
check("threshold device: some failures were recorded before the target streak",
      "MISMATCH" in out2.getvalue())
check("threshold device: eventually reports success in the summary",
      "consecutive clean write(s)" in out2.getvalue())

# --- Scenario 3: never recovers -> should hit --max-settle and exit(1)
#     rather than loop forever or silently return.
class FakeDevAlwaysBad(FakeDevBase):
    def verify_write_full(self, slot, fb, skel):
        roundtrip = gp200.build_prst_from_dump(dump_bad, "x", skel)
        mismatches = gp200.diff_prst_content(fb, roundtrip)
        return (len(mismatches) == 0), mismatches, gp200.prst_file_name(roundtrip), roundtrip

dev3 = FakeDevAlwaysBad()
gp200.Device = lambda *a, **kw: dev3
args3 = make_args(start_settle=1.0, step=0.5, max_settle=2.0, target_streak=20)
out3 = io.StringIO()
raised = False
try:
    with contextlib.redirect_stdout(out3):
        gp200.cmd_calibrate_settle(args3)
except SystemExit as e:
    raised = True
    check("always-bad device: SystemExit carries a message, not a bare code",
          isinstance(e.code, str) and "settle-time theory" in e.code)
check("always-bad device: hitting --max-settle raises SystemExit instead of looping/exiting silently",
      raised)
check("always-bad device: connection was still closed even on the error path", dev3.closed)

# --- Scenario 4: --max-attempts safety cap also raises SystemExit (not just
#     an infinite loop) even if settle never happens to cross --max-settle
#     (e.g. a huge --step relative to a tiny --max-settle isn't required;
#     here we just force many tries by keeping it bad but with a very high
#     --max-settle and a low --max-attempts).
dev4 = FakeDevAlwaysBad()
gp200.Device = lambda *a, **kw: dev4
args4 = make_args(start_settle=1.0, step=0.01, max_settle=100.0, max_attempts=5, target_streak=20)
raised4 = False
try:
    with contextlib.redirect_stdout(io.StringIO()):
        gp200.cmd_calibrate_settle(args4)
except SystemExit as e:
    raised4 = True
    check("max-attempts cap: SystemExit message mentions the attempt cap",
          isinstance(e.code, str) and "attempts" in e.code)
check("max-attempts cap: stops after --max-attempts rather than running forever", raised4)
check("max-attempts cap: total writes bounded close to --max-attempts",
      dev4.write_calls <= args4.max_attempts + 1)

# --- Scenario 5: mismatches concentrated on ONE offset should be tallied and
#     called out by name in the final summary -- this is the real-world
#     pattern from the user's actual hardware run (every failure landed on
#     file offset 0x9F, across several different settle delays).
REPEAT_OFFSET = 0x9F
FAIL_ON_ATTEMPTS = {2, 5, 9}  # a few scattered failures, all at the same offset,
                              # with the last one early enough that a streak of
                              # target_streak clean writes can still follow it
class FakeDevSameOffsetAlways(FakeDevBase):
    """Fails on a few specific attempts, always at the same offset, succeeds
    otherwise -- mimics an intermittent-but-localized real bug."""
    def verify_write_full(self, slot, fb, skel):
        if self.write_calls in FAIL_ON_ATTEMPTS:
            bad = bytearray(dump_clean)
            bad[REPEAT_OFFSET - gp200.CONTENT_FILE_START] ^= 0xFF
            roundtrip = gp200.build_prst_from_dump(bytes(bad), "x", skel)
        else:
            roundtrip = gp200.build_prst_from_dump(dump_clean, "x", skel)
        mismatches = gp200.diff_prst_content(fb, roundtrip)
        return (len(mismatches) == 0), mismatches, gp200.prst_file_name(roundtrip), roundtrip

dev5 = FakeDevSameOffsetAlways()
gp200.Device = lambda *a, **kw: dev5
args5 = make_args(target_streak=10, max_attempts=60)
out5 = io.StringIO()
with contextlib.redirect_stdout(out5):
    gp200.cmd_calibrate_settle(args5)
text5 = out5.getvalue()
check("same-offset device: the offset that always mismatched is named in the summary",
      f"0x{REPEAT_OFFSET:04X}" in text5 and "pre-effects header byte" in text5)
check("same-offset device: summary flags it as always the same offset",
      "always the SAME offset" in text5)

# --- Scenario 6: Ctrl-C mid-run prints a summary (with whatever was tallied
#     so far) instead of an unhandled traceback, and still exits (non-zero),
#     and still closes the device connection -- exactly what the real
#     transcript showed was missing (a bare KeyboardInterrupt traceback).
class FakeDevInterruptsThenBad(FakeDevBase):
    def write_slot(self, slot, fb, currently_active, commit=False, settle_s=1.0):
        super().write_slot(slot, fb, currently_active, commit=commit, settle_s=settle_s)
        if self.write_calls == 3:
            raise KeyboardInterrupt()
    def verify_write_full(self, slot, fb, skel):
        bad = bytearray(dump_clean)
        bad[REPEAT_OFFSET - gp200.CONTENT_FILE_START] ^= 0xFF
        roundtrip = gp200.build_prst_from_dump(bytes(bad), "x", skel)
        mismatches = gp200.diff_prst_content(fb, roundtrip)
        return (len(mismatches) == 0), mismatches, gp200.prst_file_name(roundtrip), roundtrip

dev6 = FakeDevInterruptsThenBad()
gp200.Device = lambda *a, **kw: dev6
args6 = make_args(target_streak=20)
out6 = io.StringIO()
raised6 = False
try:
    with contextlib.redirect_stdout(out6):
        gp200.cmd_calibrate_settle(args6)
except SystemExit as e:
    raised6 = True
    check("Ctrl-C: SystemExit says it was interrupted, not a bare traceback",
          isinstance(e.code, str) and "Interrupted" in e.code)
except KeyboardInterrupt:
    pass
check("Ctrl-C: raises a clean SystemExit rather than propagating KeyboardInterrupt", raised6)
check("Ctrl-C: still prints a summary (with whatever was tallied) before exiting",
      "Stopped after" in out6.getvalue())
check("Ctrl-C: device connection is still closed", dev6.closed)

gp200.Device = orig_device
gp200.Path.read_bytes = orig_read_bytes
gp200._resolve_verify_skeleton = orig_resolve_verify_skeleton
gp200.confirm_overwrite = orig_confirm_overwrite

print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("ALL CALIBRATE-SETTLE CHECKS PASSED")
