import argparse, importlib.util, io, contextlib, os, tempfile, atexit, shutil, time
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

# This test deliberately drives cmd_soak through real write-failure and
# mismatch scenarios, which -- via the real, unmocked print_failure_
# diagnostics -- write actual failed_verify_*.prst files to whatever the
# current directory happens to be. Found by actually running run_tests.py
# from a real project folder and checking what got left behind afterward,
# not assumed: this test was the one file, of the several capable of
# reaching that code path, that hadn't already been isolated like this.
_tmp = tempfile.mkdtemp(prefix="gp200_test_")
atexit.register(shutil.rmtree, _tmp, ignore_errors=True)
os.chdir(_tmp)

skeleton = gp200.resolve_skeleton_bytes(None)
file_bytes = skeleton
dump_clean = bytes(file_bytes)[gp200.CONTENT_FILE_START:gp200.CHECKSUM_OFF]
REPEAT_OFFSET = 0x9F
bad_bytes = bytearray(file_bytes)
bad_bytes[REPEAT_OFFSET] ^= 0xFF
dump_bad = bytes(bad_bytes)[gp200.CONTENT_FILE_START:gp200.CHECKSUM_OFF]

orig_device = gp200.Device
orig_read_bytes = gp200.Path.read_bytes
orig_resolve_verify_skeleton = gp200._resolve_verify_skeleton
orig_confirm_overwrite = gp200.confirm_overwrite
gp200.Path.read_bytes = lambda self: bytes(file_bytes)
gp200._resolve_verify_skeleton = lambda: skeleton
gp200.confirm_overwrite = lambda dev, slot, force: True


def make_args(**overrides):
    ns = argparse.Namespace(slot="1-A", file="unused.prst", port=None, debug=False, force=True,
                             commit=False, count=10, settle=1.0, label=None,
                             confirm_reads=0, stop_on_failure=False)
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


class FakeDevBase:
    def __init__(self):
        self.write_calls = 0
        self.settle_values = []
        self.closed = False
        # _confirm_discrepancy passes build_prst_from_dump(..., debug=dev.debug)
        # as of 2026-09-29 (suppressing its "(overlaid ...)" line unless -d is
        # used) -- every Device stand-in needs this attribute now, not just
        # real Device instances.
        self.debug = False
    def write_slot(self, slot, fb, currently_active, commit=False, settle_s=1.0):
        self.write_calls += 1
        self.settle_values.append(settle_s)
    def close(self):
        self.closed = True


# --- always clean: 0/N failures, delay never changes, no exit/error ---
class FakeDevClean(FakeDevBase):
    def verify_write_full(self, slot, fb, skel, ignore_dead_bytes=True):  # accepts, ignores -- these fakes already do a strict (unfiltered) comparison
        roundtrip = gp200.build_prst_from_dump(dump_clean, "x", skel)
        return (len(gp200.diff_prst_content(fb, roundtrip)) == 0), [], gp200.prst_file_name(roundtrip), roundtrip

dev1 = FakeDevClean()
gp200.Device = lambda *a, **kw: dev1
out1 = io.StringIO()
with contextlib.redirect_stdout(out1):
    gp200.cmd_soak(make_args(count=10, label="stock supply, run 1"))
text1 = out1.getvalue()
check("soak: runs exactly --count cycles", dev1.write_calls == 10)
check("soak: settle delay never changes across a run", all(s == 1.0 for s in dev1.settle_values))
check("soak: reports 0/10 failed", "0/10 failed (0%)" in text1)
check("soak: includes the --label in the output", "[stock supply, run 1]" in text1)
check("soak: includes environment info", "python:" in text1 and "OS:" in text1)
check("soak: closes the device", dev1.closed)

# --- some failures at a KNOWN fixed offset: rate reported correctly, delay
#     still never changes (unlike calibrate-settle), and it's NOT fatal --
#     no SystemExit even though failures occurred.
FAIL_ATTEMPTS = {3, 7}
class FakeDevSomeFail(FakeDevBase):
    def verify_write_full(self, slot, fb, skel, ignore_dead_bytes=True):  # accepts, ignores -- these fakes already do a strict (unfiltered) comparison
        dump = dump_bad if self.write_calls in FAIL_ATTEMPTS else dump_clean
        roundtrip = gp200.build_prst_from_dump(dump, "x", skel)
        mismatches = gp200.diff_prst_content(fb, roundtrip)
        return (len(mismatches) == 0), mismatches, gp200.prst_file_name(roundtrip), roundtrip

dev2 = FakeDevSomeFail()
gp200.Device = lambda *a, **kw: dev2
out2 = io.StringIO()
no_exit = True
try:
    with contextlib.redirect_stdout(out2):
        gp200.cmd_soak(make_args(count=10, settle=1.0, label="battery, run 1"))
except SystemExit:
    no_exit = False
text2 = out2.getvalue()
check("soak: a run with some failures does NOT raise SystemExit (not fatal by design)", no_exit)
check("soak: settle delay stays fixed even when failures occur (unlike calibrate-settle)",
      all(s == 1.0 for s in dev2.settle_values))
check("soak: reports the correct failure count/rate", "2/10 failed (20%)" in text2)
check("soak: tallies the specific offset that mismatched",
      f"0x{REPEAT_OFFSET:04X}" in text2)

# --- --settle is honored and passed straight through to write_slot ---
dev3 = FakeDevClean()
gp200.Device = lambda *a, **kw: dev3
with contextlib.redirect_stdout(io.StringIO()):
    gp200.cmd_soak(make_args(count=5, settle=2.5))
check("soak: --settle is passed through to every write_slot call unchanged",
      dev3.write_calls == 5 and all(s == 2.5 for s in dev3.settle_values))

# --- --confirm-reads: a discrepancy that turns out to be STORED (every
#     confirmation re-read agrees with the bad value) ---
class FakeDevFailThenStored(FakeDevBase):
    def __init__(self):
        super().__init__()
        self.read_calls = 0
    def verify_write_full(self, slot, fb, skel, ignore_dead_bytes=True):  # accepts, ignores -- these fakes already do a strict (unfiltered) comparison
        roundtrip = gp200.build_prst_from_dump(dump_bad, "x", skel)
        mismatches = gp200.diff_prst_content(fb, roundtrip)
        return (len(mismatches) == 0), mismatches, gp200.prst_file_name(roundtrip), roundtrip
    def read_dump(self, slot):
        self.read_calls += 1
        return dump_bad  # every confirmation re-read agrees with the bad value

dev4 = FakeDevFailThenStored()
gp200.Device = lambda *a, **kw: dev4
out6 = io.StringIO()
with contextlib.redirect_stdout(out6):
    gp200.cmd_soak(make_args(count=3, confirm_reads=4))
text6 = out6.getvalue()
check("soak --confirm-reads: does the extra no-write re-reads on a mismatch",
      dev4.read_calls == 4 * 3)  # 3 cycles, all fail, 4 confirm-reads each
check("soak --confirm-reads: a discrepancy that survives every re-read is reported as STORED",
      "looks STORED on the device" in text6)

# --- --confirm-reads: a discrepancy that turns out to be transient (every
#     confirmation re-read now matches the SOURCE file instead) ---
class FakeDevFailThenTransient(FakeDevBase):
    def verify_write_full(self, slot, fb, skel, ignore_dead_bytes=True):  # accepts, ignores -- these fakes already do a strict (unfiltered) comparison
        roundtrip = gp200.build_prst_from_dump(dump_bad, "x", skel)
        mismatches = gp200.diff_prst_content(fb, roundtrip)
        return (len(mismatches) == 0), mismatches, gp200.prst_file_name(roundtrip), roundtrip
    def read_dump(self, slot):
        return dump_clean  # confirmation re-reads now agree with the source

dev5 = FakeDevFailThenTransient()
gp200.Device = lambda *a, **kw: dev5
out7 = io.StringIO()
with contextlib.redirect_stdout(out7):
    gp200.cmd_soak(make_args(count=1, confirm_reads=3))
check("soak --confirm-reads: a discrepancy that vanishes on re-read is reported as read-side noise",
      "looks like read-side noise" in out7.getvalue())

# --- --stop-on-failure: halts immediately after the first mismatch (and its
#     confirmation reads, if any), rather than running the full --count ---
dev6 = FakeDevFailThenStored()
gp200.Device = lambda *a, **kw: dev6
out8 = io.StringIO()
with contextlib.redirect_stdout(out8):
    gp200.cmd_soak(make_args(count=10, confirm_reads=1, stop_on_failure=True))
check("soak --stop-on-failure: stops after the first mismatch instead of running all --count cycles",
      dev6.write_calls == 1)
check("soak --stop-on-failure: says why it stopped early",
      "stopping after cycle 1/10" in out8.getvalue())
# A real hardware run (2026-09-27) reported "Result: 1/20 failed (5%)" after
# --stop-on-failure halted the run at cycle 3/20 -- misleading, since only 3
# cycles ever ran. The summary must report the rate over cycles actually
# attempted, not the configured --count.
check("soak --stop-on-failure: summary rate is over cycles ACTUALLY attempted, not --count "
      "(1 attempted, 1 failed -> 1/1, 100%, not 1/10)",
      "Result: 1/1 failed (100%)" in out8.getvalue())
check("soak --stop-on-failure: summary notes the run stopped early",
      "1 attempted before stopping" in out8.getvalue())

# A full, non-early-stopped run should NOT show the "attempted before
# stopping" note at all -- cycles_run == args.count in that case.
check("soak: a full run's summary carries no 'stopped early' note",
      "attempted before stopping" not in text1)

# --confirm-reads defaults to 0 (opt-in, no behavior change unless asked for)
dev7 = FakeDevSomeFail()
gp200.Device = lambda *a, **kw: dev7
out9 = io.StringIO()
with contextlib.redirect_stdout(out9):
    gp200.cmd_soak(make_args(count=10, settle=1.0))
check("soak: --confirm-reads defaults to off (no read_dump calls, no 'Confirming' text)",
      "Confirming" not in out9.getvalue())

# --- Regression guard (2026-09-29): cmd_soak exists specifically to
#     characterize write reliability across many cycles -- it must call the
#     REAL verify_write_full with ignore_dead_bytes=False, so a mismatch
#     confined to 0x43/0x9F (DEAD_BYTE_FILE_OFFSETS) still counts as a real
#     failure here, unlike the relaxed default an ordinary `upload` now
#     gets. Uses the real Device.verify_write_full (not a hand-rolled fake),
#     so this actually exercises cmd_soak's own call site. ---
class FakeDevRealVerifyDeadByte:
    # orig_device, not gp200.Device -- by this point gp200.Device has been
    # repeatedly reassigned to a lambda by earlier scenarios.
    verify_write_full = orig_device.verify_write_full
    read_dump_confirmed = orig_device.read_dump_confirmed
    _dbg = orig_device._dbg

    def __init__(self):
        self.debug = False
        self._t0 = time.monotonic()
        self.closed = False
        bad = bytearray(dump_clean)
        bad[0x43 - gp200.CONTENT_FILE_START] ^= 0xFF
        bad[0x9F - gp200.CONTENT_FILE_START] ^= 0xFF
        self.dump = bytes(bad)  # differs from file_bytes ONLY at the dead-byte offsets

    def write_slot(self, slot, fb, currently_active, commit=False, settle_s=1.0):
        pass

    def read_dump(self, slot):
        return self.dump  # same every time -> read_dump_confirmed confirms on read 2

    def close(self):
        self.closed = True


dev_dead_soak = FakeDevRealVerifyDeadByte()
gp200.Device = lambda *a, **kw: dev_dead_soak
out_dead_soak = io.StringIO()
with contextlib.redirect_stdout(out_dead_soak):
    gp200.cmd_soak(make_args(count=3))
check("soak: a mismatch confined to DEAD_BYTE_FILE_OFFSETS is still counted as a "
      "real failure here, not silently treated as OK -- proves ignore_dead_bytes=False "
      "is actually reaching the real verify_write_full",
      "3/3 failed" in out_dead_soak.getvalue())

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
print("ALL SOAK CHECKS PASSED")
