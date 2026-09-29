"""Prompted directly by real-world confusion (2026-09-29): `list` felt much
slower in practice than `export --all`, which is backwards -- export does a
confirmed multi-chunk read per slot, list does one small chunk. Before
chasing that on real hardware, the --debug trace needed to actually show
*how long each read took*, not just *that* a request/response happened: it
had no timestamps at all, so answering "is this slot-by-slot latency, or is
something timing out and eating its retry budget" meant eyeballing raw
message dumps with no timing information in them whatsoever.

Covers the fix: every --debug line now carries an elapsed-time prefix
(Device._dbg, timed from connection via Device._t0), and _drain_matching
additionally reports the actual measured round-trip on both success
("received in Xs") and timeout ("timed out after Xs of Ys budget") --
the latter changed from parroting back the configured timeout_s (which
tells you nothing new) to the real elapsed time (which, combined with the
polling loop's ~10ms granularity, is the actual evidence). Also covers the
new unconditional (not --debug-gated) one-line summary `list` and `export`
now print, since a single total is cheap enough to always show and answers
the "how long did this take" question without needing a debug trace at all.
"""
import argparse, contextlib, importlib.util, io, re, time
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


# --- _dbg: timestamped, and only when debug is on ---
dev = gp200.Device.__new__(gp200.Device)
dev._t0 = time.monotonic()
dev.debug = False
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    dev._dbg("should not appear")
check("_dbg prints nothing when debug is off",
      buf.getvalue() == "")

dev.debug = True
buf2 = io.StringIO()
with contextlib.redirect_stdout(buf2):
    dev._dbg("hello")
out2 = buf2.getvalue()
check("_dbg prints the message when debug is on",
      "hello" in out2)
check("_dbg prefixes an elapsed-seconds timestamp, e.g. '[+  0.001s]'",
      re.search(r"\[\+\s*\d+\.\d{3}s\]", out2) is not None)


# --- _drain_matching: reports the real measured round-trip, not just the
#     configured budget, on both the success and timeout paths ---
class FakeMsg:
    def __init__(self, data):
        self.type = "sysex"
        self.data = data


class FakePort:
    def __init__(self, queue):
        self.queue = list(queue)

    def receive(self, block=False):
        return self.queue.pop(0) if self.queue else None


def make_data_between(cmd, sub, off_lo, off_hi, payload_len=10):
    full = bytearray()
    full += gp200.HEADER
    full += bytes([cmd, sub, 0x00, off_lo, off_hi])
    full += bytes([0x01] * payload_len)
    full.append(0xF7)
    return bytes(full[1:-1])


dev3 = gp200.Device.__new__(gp200.Device)
dev3._t0 = time.monotonic()
dev3.debug = True
dev3.inport = FakePort([FakeMsg(make_data_between(0x12, 0x18, 0x00, 0x00))])
buf3 = io.StringIO()
with contextlib.redirect_stdout(buf3):
    result = dev3._drain_matching(0x12, 0x18, 1, timeout_s=1.0, require_offset0=True)
check("_drain_matching still returns the matched chunk (timing additions "
      "didn't change the actual logic)",
      result is not None and len(result) == 1)
check("_drain_matching reports actual measured latency on success, not "
      "just 'it worked'",
      re.search(r"received in \d+\.\d{3}s", buf3.getvalue()) is not None)

dev4 = gp200.Device.__new__(gp200.Device)
dev4._t0 = time.monotonic()
dev4.debug = True
dev4.inport = FakePort([])  # nothing ever arrives -> times out
buf4 = io.StringIO()
with contextlib.redirect_stdout(buf4):
    result4 = dev4._drain_matching(0x12, 0x18, 1, timeout_s=0.05, require_offset0=True)
check("_drain_matching returns None on a genuine timeout",
      result4 is None)
check("_drain_matching's timeout message shows the ACTUAL elapsed time "
      "(not just echoing the configured budget back)",
      re.search(r"timed out after \d+\.\d{3}s of 0\.05s budget", buf4.getvalue()) is not None)


# --- cmd_list: prints an unconditional (non --debug) total, and counts
#     timeouts, without needing to touch real hardware ---
orig_device = gp200.Device


class FakeListDevice:
    """cmd_list calls read_name_via_dump, not read_name, as of 2026-09-29
    (see gp200.Device.read_name_via_dump's docstring for the real-hardware
    evidence behind the switch) -- this fake only needs to implement
    whichever one cmd_list actually calls."""
    def __init__(self):
        self.closed = False

    def read_name_via_dump(self, slot):
        if slot == 3:
            raise TimeoutError("no response")
        return f"Patch {slot}"

    def close(self):
        self.closed = True


gp200.Device = lambda *a, **kw: FakeListDevice()
args = argparse.Namespace(port=None, debug=False)
buf5 = io.StringIO()
with contextlib.redirect_stdout(buf5):
    gp200.cmd_list(args)
out5 = buf5.getvalue()
gp200.Device = orig_device

check("list: prints a summary line even without --debug",
      re.search(rf"{gp200.TOTAL_SLOTS} slots read in \d+\.\d+s", out5) is not None)
check("list: counts and reports the one simulated timeout",
      "(1 timeout(s))" in out5)
# 2026-09-29, direct feedback: "does list output print an error if a slot
# could not be read or just omit it? I think it should say 'Error reading
# patch <patch number>'" -- the failed slot's label must still be printed
# (not omitted), paired with a failure message that's impossible to mistake
# for an actual patch name, using the same plain-language phrasing `export`
# already uses for the same underlying failure.
check("list: a failed slot still prints its label -- it's never silently omitted",
      f"{gp200.slot_to_label(3):>4}" in out5)
check("list: a failed slot's name column is an unmistakable error, not a "
      "blend-in placeholder like the old '(no response)'",
      "*** error reading this patch: no response from the device ***" in out5)


print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("ALL DEBUG-TIMING CHECKS PASSED")
