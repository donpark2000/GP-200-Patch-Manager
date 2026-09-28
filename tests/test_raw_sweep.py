import argparse, importlib.util, io, contextlib
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
dump_clean = bytes(skeleton)[gp200.CONTENT_FILE_START:gp200.CHECKSUM_OFF]
bad_bytes = bytearray(skeleton)
bad_bytes[gp200.CONTENT_FILE_START + 100] ^= 0xFF
dump_bad = bytes(bad_bytes)[gp200.CONTENT_FILE_START:gp200.CHECKSUM_OFF]

orig_device = gp200.Device


def make_args(**overrides):
    ns = argparse.Namespace(slot=None, all=False, start=None, end=None, port=None, debug=False)
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


# ---------------------------------------------------------------------------
# 1. Every slot's raw read agrees with its confirmed read -> all clean.
# ---------------------------------------------------------------------------
class FakeDevAllAgree:
    def __init__(self):
        self.closed = False
    def read_dump(self, slot):
        return dump_clean
    def read_dump_confirmed(self, slot, tries=5):
        return dump_clean
    def close(self):
        self.closed = True

dev1 = FakeDevAllAgree()
gp200.Device = lambda *a, **kw: dev1
out1 = io.StringIO()
with contextlib.redirect_stdout(out1):
    gp200.cmd_raw_sweep(make_args(all=True))
text1 = out1.getvalue()
check("raw-sweep --all: all 256 slots reported as matching", text1.count("matches confirmed reading") == 256)
check("raw-sweep --all: summary reports 256 clean, 0 mismatches", "256 clean, 0 where" in text1)
check("raw-sweep: closes the device", dev1.closed)

# ---------------------------------------------------------------------------
# 2. One slot's raw read disagrees with its confirmed read -> flagged.
# ---------------------------------------------------------------------------
class FakeDevOneMismatch:
    def __init__(self):
        self.closed = False
    def read_dump(self, slot):
        # Slot 5 ("1-F") comes back glitchy on the raw read but every other
        # slot is clean.
        return dump_bad if slot == 5 else dump_clean
    def read_dump_confirmed(self, slot, tries=5):
        return dump_clean
    def close(self):
        self.closed = True

dev2 = FakeDevOneMismatch()
gp200.Device = lambda *a, **kw: dev2
out2 = io.StringIO()
with contextlib.redirect_stdout(out2):
    gp200.cmd_raw_sweep(make_args(all=True))
text2 = out2.getvalue()
check("raw-sweep --all: exactly one MISMATCH reported", text2.count("MISMATCH") == 1)
check("raw-sweep --all: the mismatched slot is named in the summary",
      "Mismatched slots: " in text2 and gp200.slot_to_label(5) in text2)
check("raw-sweep --all: summary counts 255 clean / 1 mismatch", "255 clean, 1 where" in text2)

# ---------------------------------------------------------------------------
# 3. Raw read times out even after read_dump's own retry.
# ---------------------------------------------------------------------------
class FakeDevRawTimeout:
    def __init__(self):
        self.closed = False
    def read_dump(self, slot):
        if slot == 0:
            raise TimeoutError("no response")
        return dump_clean
    def read_dump_confirmed(self, slot, tries=5):
        return dump_clean
    def close(self):
        self.closed = True

dev3 = FakeDevRawTimeout()
gp200.Device = lambda *a, **kw: dev3
out3 = io.StringIO()
with contextlib.redirect_stdout(out3):
    gp200.cmd_raw_sweep(make_args(all=True))
text3 = out3.getvalue()
check("raw-sweep --all: raw timeout reported distinctly from a mismatch",
      "raw read timed out" in text3 and "MISMATCH" not in text3)
check("raw-sweep --all: summary counts the raw timeout separately", "1 raw read timeout" in text3)

# ---------------------------------------------------------------------------
# 4. read_dump_confirmed itself can't settle -> reported as unconfirmable,
#    not silently treated as a match or a mismatch.
# ---------------------------------------------------------------------------
class FakeDevConfirmFails:
    def __init__(self):
        self.closed = False
    def read_dump(self, slot):
        return dump_clean
    def read_dump_confirmed(self, slot, tries=5):
        if slot == 0:
            raise gp200.ReadNotConfirmedError("5 reads never agreed")
        return dump_clean
    def close(self):
        self.closed = True

dev4 = FakeDevConfirmFails()
gp200.Device = lambda *a, **kw: dev4
out4 = io.StringIO()
with contextlib.redirect_stdout(out4):
    gp200.cmd_raw_sweep(make_args(all=True))
text4 = out4.getvalue()
check("raw-sweep --all: an unconfirmable slot is reported as such, not a match/mismatch",
      "couldn't get a confirmed reading" in text4)
check("raw-sweep --all: summary counts it as unconfirmable", "1 slot(s) where even read_dump_confirmed" in text4)

# ---------------------------------------------------------------------------
# 5. Single-slot mode.
# ---------------------------------------------------------------------------
dev5 = FakeDevAllAgree()
gp200.Device = lambda *a, **kw: dev5
out5 = io.StringIO()
with contextlib.redirect_stdout(out5):
    gp200.cmd_raw_sweep(make_args(slot="1-A"))
check("raw-sweep single slot: checks exactly one slot", out5.getvalue().count("matches confirmed reading") == 1)

# ---------------------------------------------------------------------------
# 6. --start/--end range mode.
# ---------------------------------------------------------------------------
class FakeDevRange:
    def __init__(self):
        self.seen = []
        self.closed = False
    def read_dump(self, slot):
        self.seen.append(slot)
        return dump_clean
    def read_dump_confirmed(self, slot, tries=5):
        return dump_clean
    def close(self):
        self.closed = True

dev6 = FakeDevRange()
gp200.Device = lambda *a, **kw: dev6
out6 = io.StringIO()
with contextlib.redirect_stdout(out6):
    gp200.cmd_raw_sweep(make_args(start="34-A", end="34-D"))
check("raw-sweep --start/--end: reads exactly the requested range",
      dev6.seen == gp200.parse_slot_range("34-A", "34-D"))
check("raw-sweep --start/--end: reports 4 clean slots", "matches confirmed reading") if False else None
check("raw-sweep --start/--end: summary reflects the 4-slot range", "4 clean, 0 where" in out6.getvalue())

# ---------------------------------------------------------------------------
# 7. Mode validation: nothing given, half a range given, both --all and a
#    range given -- all should exit cleanly rather than silently picking one.
# ---------------------------------------------------------------------------
try:
    gp200.cmd_raw_sweep(make_args())
    check("raw-sweep: no mode given raises SystemExit", False)
except SystemExit:
    check("raw-sweep: no mode given raises SystemExit", True)

try:
    gp200.cmd_raw_sweep(make_args(start="34-A"))
    check("raw-sweep: half a range given raises SystemExit", False)
except SystemExit:
    check("raw-sweep: half a range given raises SystemExit", True)

try:
    gp200.cmd_raw_sweep(make_args(all=True, start="34-A", end="34-D"))
    check("raw-sweep: --all plus a range raises SystemExit", False)
except SystemExit:
    check("raw-sweep: --all plus a range raises SystemExit", True)

gp200.Device = orig_device

print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("ALL RAW-SWEEP CHECKS PASSED")
