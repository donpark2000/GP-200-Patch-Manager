import argparse, contextlib, importlib.util, io
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

skeleton = gp200.resolve_skeleton_bytes(None)
dump_base = bytearray(bytes(skeleton)[gp200.CONTENT_FILE_START:gp200.CHECKSUM_OFF])

OFF_A = 0x43
OFF_B = 0x9F
IDX_A = OFF_A - gp200.CONTENT_FILE_START
IDX_B = OFF_B - gp200.CONTENT_FILE_START


def dump_with(a=None, b=None):
    d = bytearray(dump_base)
    if a is not None:
        d[IDX_A] = a
    if b is not None:
        d[IDX_B] = b
    return bytes(d)


# ---------------------------------------------------------------------------
# _parse_offset_list
# ---------------------------------------------------------------------------
check("_parse_offset_list: parses hex offsets in order",
      gp200._parse_offset_list("0x43,0x9F") == [0x43, 0x9F])
check("_parse_offset_list: accepts decimal too",
      gp200._parse_offset_list("67, 159") == [67, 159])
check("_parse_offset_list: single offset",
      gp200._parse_offset_list("0x100") == [0x100])
try:
    gp200._parse_offset_list("0x43,0x43")
    check("_parse_offset_list: rejects a duplicate offset", False)
except ValueError:
    check("_parse_offset_list: rejects a duplicate offset", True)
try:
    gp200._parse_offset_list("not-a-number")
    check("_parse_offset_list: rejects garbage", False)
except ValueError:
    check("_parse_offset_list: rejects garbage", True)
try:
    gp200._parse_offset_list("")
    check("_parse_offset_list: rejects an empty list", False)
except ValueError:
    check("_parse_offset_list: rejects an empty list", True)


# ---------------------------------------------------------------------------
# cmd_drift, against a fake device that hands back a scripted sequence of
# raw dumps -- no writes involved anywhere, matching the real command's own
# "no-write reads only" design.
# ---------------------------------------------------------------------------
class FakeDevDrift:
    def __init__(self, dumps):
        self.dumps = list(dumps)
        self.closed = False
    def read_dump(self, slot):
        if not self.dumps:
            raise TimeoutError("out of scripted reads")
        d = self.dumps.pop(0)
        if d is None:
            raise TimeoutError("(scripted no response)")
        return d
    def close(self):
        self.closed = True


def make_drift_args(**overrides):
    ns = argparse.Namespace(slot="1-A", port=None, debug=False,
                             offsets="0x43,0x9F", count=5, interval=0.0)
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


orig_device = gp200.Device
orig_sleep_remaining = gp200._sleep_remaining
gp200._sleep_remaining = lambda *a, **kw: None  # keep the test instant, no real pacing needed

# A steady counter at 0x43 (0x00, 0x01, 0x02, 0x03, 0x04), 0x9F held fixed --
# the shape a per-read counter on ONE tracked byte would produce.
dumps_counter = [dump_with(a=n, b=0x00) for n in range(5)]
dev = FakeDevDrift(dumps_counter)
gp200.Device = lambda *a, **kw: dev
out = io.StringIO()
with contextlib.redirect_stdout(out):
    gp200.cmd_drift(make_drift_args())
text = out.getvalue()
check("cmd_drift: closes the device connection", dev.closed)
check("cmd_drift: reports 5 successful reads, 0 timeouts",
      "5 successful read(s), 0 timeout(s)" in text)
check("cmd_drift: 0x43 shows 5 distinct values (steady increment)",
      "0x0043" in text and "5 distinct value(s) seen" in text)
check("cmd_drift: 0x43 shows 4 changes across 5 reads",
      "4 change(s) across 5 read(s)" in text)
check("cmd_drift: 0x9F never changed and is reported as such",
      "never changed across 5 read(s)" in text)
check("cmd_drift: a steady increment is reported as all increase(s), no decrease(s)",
      "4 increase(s)-or-wrap, 0 decrease(s)" in text)
check("cmd_drift: prints per-read-rate and per-second-rate lines for the changing byte",
      "change(s) per read" in text and "change(s) per second" in text)
check("cmd_drift: prints the closing guidance about varying --interval",
      "tell a per-read counter apart from a free-running clock" in text)

# A value that jumps around unpredictably (not monotonic) -- should be
# reported with a mix of increases and decreases, not "mostly increasing".
dumps_noisy = [dump_with(a=v, b=0x00) for v in (0x10, 0xB0, 0x11, 0x05, 0x11)]
dev2 = FakeDevDrift(dumps_noisy)
gp200.Device = lambda *a, **kw: dev2
out2 = io.StringIO()
with contextlib.redirect_stdout(out2):
    gp200.cmd_drift(make_drift_args(offsets="0x43"))
text2 = out2.getvalue()
check("cmd_drift: a noisy, non-monotonic sequence reports at least one decrease",
      "decrease(s)" in text2 and "0 decrease(s)" not in text2)

# A high-to-low drop of >200 is treated as a plausible 0xFF->0x00-style wrap,
# not counted as a decrease.
dumps_wrap = [dump_with(a=v, b=0x00) for v in (0xFC, 0xFD, 0xFE, 0x00, 0x01)]
dev3 = FakeDevDrift(dumps_wrap)
gp200.Device = lambda *a, **kw: dev3
out3 = io.StringIO()
with contextlib.redirect_stdout(out3):
    gp200.cmd_drift(make_drift_args(offsets="0x43"))
text3 = out3.getvalue()
check("cmd_drift: a wraparound (0xFE -> 0x00) is counted as increase-or-wrap, not a decrease",
      "4 increase(s)-or-wrap, 0 decrease(s)" in text3)

# Timeouts are logged and counted, and don't stop the run.
dumps_with_timeout = [dump_with(a=1, b=0), None, dump_with(a=2, b=0)]
dev4 = FakeDevDrift(dumps_with_timeout)
gp200.Device = lambda *a, **kw: dev4
out4 = io.StringIO()
with contextlib.redirect_stdout(out4):
    gp200.cmd_drift(make_drift_args(count=3))
text4 = out4.getvalue()
check("cmd_drift: a mid-run timeout is logged as '(no response)'", "(no response)" in text4)
check("cmd_drift: reports 2 successful reads, 1 timeout after a scripted timeout",
      "2 successful read(s), 1 timeout(s)" in text4)

# Tracking a single custom offset (not the 0x43/0x9F default) works too.
dumps_custom = [dump_with(), dump_with()]
custom_off = 0x100
custom_idx = custom_off - gp200.CONTENT_FILE_START
d0 = bytearray(dumps_custom[0]); d0[custom_idx] = 0x07; dumps_custom[0] = bytes(d0)
d1 = bytearray(dumps_custom[1]); d1[custom_idx] = 0x08; dumps_custom[1] = bytes(d1)
dev5 = FakeDevDrift(dumps_custom)
gp200.Device = lambda *a, **kw: dev5
out5 = io.StringIO()
with contextlib.redirect_stdout(out5):
    gp200.cmd_drift(make_drift_args(offsets="0x100", count=2))
text5 = out5.getvalue()
check("cmd_drift: an arbitrary --offsets value (not the default pair) is tracked correctly",
      "0x0100" in text5 and "1 change(s) across 2 read(s)" in text5)

# An offset before the dump-covered region is rejected before opening a
# connection at all (mirrors label_to_slot's own validate-before-connect
# pattern elsewhere in this file).
gp200.Device = lambda *a, **kw: (_ for _ in ()).throw(
    AssertionError("Device should never be constructed for an out-of-range offset"))
try:
    gp200.cmd_drift(make_drift_args(offsets="0x10"))
    check("cmd_drift: rejects an offset before CONTENT_FILE_START", False)
except SystemExit:
    check("cmd_drift: rejects an offset before CONTENT_FILE_START", True)

gp200.Device = orig_device
gp200._sleep_remaining = orig_sleep_remaining

print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("ALL DRIFT CHECKS PASSED")
