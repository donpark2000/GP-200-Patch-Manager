import argparse, importlib.util, io, contextlib, time, zipfile
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
bad_bytes[gp200.CONTENT_FILE_START + 0x9F] ^= 0xFF
dump_bad = bytes(bad_bytes)[gp200.CONTENT_FILE_START:gp200.CHECKSUM_OFF]

import os, tempfile
import atexit, shutil
_tmp = tempfile.mkdtemp(prefix="gp200_test_")
atexit.register(shutil.rmtree, _tmp, ignore_errors=True)
os.chdir(_tmp)  # isolate the .zip/.prst files this writes

orig_device = gp200.Device
orig_resolve_skeleton = gp200.resolve_skeleton_bytes
orig_confirm_overwrite_file = gp200.confirm_overwrite_file
gp200.resolve_skeleton_bytes = lambda p: skeleton
gp200.confirm_overwrite_file = lambda path, force: True


def make_args(**overrides):
    ns = argparse.Namespace(slot="1-A", out=None, all=False, start=None, end=None,
                             skeleton=None, force=True, port=None, debug=False)
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


class FakeDevExport:
    """A single-slot read glitches once, then agrees -- read_dump_confirmed
    should paper over it and export should end up with the CLEAN value, not
    the transient bad one. A bare read_dump (the old behavior) would have a
    1-in-N chance of grabbing the glitchy read and silently saving it.

    read_dump_confirmed below calls the REAL Device.read_dump_confirmed,
    which as of 2026-09-29 gates its retry-diagnostic prints behind
    self.debug via self._dbg() (previously unconditional -- see
    PROTOCOL_NOTES.md); this fake's glitch-then-agree sequence is exactly
    the case that fires those prints, so both attributes are required now."""
    debug = False
    _dbg = gp200.Device._dbg

    def __init__(self, sequence):
        self.sequence = list(sequence)
        self.read_calls = 0
        self.closed = False
        self._t0 = time.monotonic()
    def read_dump(self, slot):
        self.read_calls += 1
        return self.sequence.pop(0)
    def read_dump_confirmed(self, slot, tries=3):
        return orig_device.read_dump_confirmed(self, slot, tries)
    def close(self):
        self.closed = True


dev = FakeDevExport([dump_bad, dump_clean, dump_clean])
gp200.Device = lambda *a, **kw: dev
out_path = Path("1A_test.prst")
if out_path.exists():
    out_path.unlink()
with contextlib.redirect_stdout(io.StringIO()):
    gp200.cmd_export(make_args(out=str(out_path)))
check("export: single-slot export used read_dump_confirmed (more than one read call)",
      dev.read_calls > 1)
check("export: saved file matches the value two reads agreed on, not the transient glitch",
      out_path.exists() and out_path.read_bytes() ==
      gp200.normalize_export_dynamic_fields(gp200.build_prst_from_dump(dump_clean, "x", skeleton)))
if out_path.exists():
    out_path.unlink()

dump_other = bytearray(skeleton)
dump_other[gp200.CONTENT_FILE_START + 0x9F] = 0x77
dump_other = bytes(dump_other)[gp200.CONTENT_FILE_START:gp200.CHECKSUM_OFF]

# --- reads that NEVER agree, single-slot export: this call site used to have
#     NO exception handling at all around read_dump_confirmed -- a real
#     failure here would have crashed with a raw traceback. It should now
#     exit cleanly with a clear message instead. ---
dev_never_agrees = FakeDevExport([dump_bad, dump_clean, dump_other])
gp200.Device = lambda *a, **kw: dev_never_agrees
out_path2 = Path("1A_test2.prst")
if out_path2.exists():
    out_path2.unlink()
exited_cleanly = False
message = ""
try:
    with contextlib.redirect_stdout(io.StringIO()):
        gp200.cmd_export(make_args(out=str(out_path2)))
except SystemExit as e:
    exited_cleanly = True
    message = str(e)
check("export: reads that never agree exit cleanly (SystemExit), not a raw crash",
      exited_cleanly)
# 2026-09-29: the exit message uses a plain-language phrase now, not the raw
# "N read(s) of X never agreed with each other" exception text -- that names
# an internal retry count that means nothing to a guitar player ("the
# message for a skipped one about 5 consecutive reads not matching is a
# debug thing"). The raw detail is still available, just gated behind
# --debug now (checked separately below), not baked into the default message.
check("export: the exit message explains the read failed in plain language, "
      "not a generic/empty error",
      "couldn't get a reliable read" in message)
check("export: the exit message does NOT expose the internal retry-count wording "
      "by default (that's --debug detail now, not a user-facing message)",
      "never agreed" not in message)
check("export: nothing was written when reads never agreed",
      not out_path2.exists())
check("export: still closed the device connection even on this failure path",
      dev_never_agrees.closed)

# --debug: the same failure, but the raw retry-count detail should still be
# fully available for anyone who asks for it.
dev_never_agrees_dbg = FakeDevExport([dump_bad, dump_clean, dump_other])
gp200.Device = lambda *a, **kw: dev_never_agrees_dbg
out_path2b = Path("1A_test2b.prst")
if out_path2b.exists():
    out_path2b.unlink()
message_dbg = ""
try:
    with contextlib.redirect_stdout(io.StringIO()):
        gp200.cmd_export(make_args(out=str(out_path2b), debug=True))
except SystemExit as e:
    message_dbg = str(e)
check("export --debug: the exit message STILL includes the raw retry-count detail "
      "when --debug is on",
      "never agreed" in message_dbg)
if out_path2b.exists():
    out_path2b.unlink()

# --- export --all: the overwrite prompt must happen BEFORE reading any
#     slot, not after all 256 -- a real ~256-slot run (2026-09-27) showed the
#     prompt appearing only at the very end, so declining (or a stray
#     keystroke) threw away a full read pass that had already completed. ---
class FakeDevAllNeverCalled:
    def __init__(self):
        self.read_calls = 0
        self.closed = False
    def read_dump_confirmed(self, slot, tries=3):
        self.read_calls += 1
        return dump_clean
    def close(self):
        self.closed = True

dev_all = FakeDevAllNeverCalled()
gp200.Device = lambda *a, **kw: dev_all
declined_confirms = []
gp200.confirm_overwrite_file = lambda path, force: (declined_confirms.append(path) or False)
out_path3 = Path("declined_all.zip")
if out_path3.exists():
    out_path3.unlink()
with contextlib.redirect_stdout(io.StringIO()):
    gp200.cmd_export(make_args(slot=None, all=True, out=str(out_path3)))
check("export --all: declining the overwrite prompt reads ZERO slots from the device "
      "(prompt now happens before the 256-slot loop, not after it)",
      dev_all.read_calls == 0)
check("export --all: the overwrite prompt was asked about the right output path",
      declined_confirms == [out_path3])
check("export --all: nothing is written to disk when the prompt is declined",
      not out_path3.exists())

# ---- export --start/--end: a range export (2026-09-27), mirroring
#      apply-template's existing range syntax. Should behave exactly like
#      --all but scoped to just the given inclusive slot range. ----
class FakeDevRangeReader:
    def __init__(self):
        self.read_calls = []
        self.closed = False
    def read_dump_confirmed(self, slot, tries=3):
        self.read_calls.append(slot)
        return dump_clean
    def close(self):
        self.closed = True

gp200.confirm_overwrite_file = lambda path, force: True
dev_range = FakeDevRangeReader()
gp200.Device = lambda *a, **kw: dev_range
out_path4 = Path("range_test.zip")
if out_path4.exists():
    out_path4.unlink()
with contextlib.redirect_stdout(io.StringIO()):
    gp200.cmd_export(make_args(slot=None, start="34-A", end="34-D", out=str(out_path4)))
expected_slots = list(range(gp200.label_to_slot("34-A"), gp200.label_to_slot("34-D") + 1))
check("export --start/--end: reads exactly the slots in that inclusive range, "
      "not all 256 and not a different range",
      dev_range.read_calls == expected_slots)
check("export --start/--end: writes a zip with one entry per slot in the range",
      out_path4.exists() and len(zipfile.ZipFile(out_path4).namelist()) == len(expected_slots))
out_path4.unlink()

# --- default output filename for a range export is derived from the range
#     when --out isn't given (mirrors --all's fixed default) ---
dev_range2 = FakeDevRangeReader()
gp200.Device = lambda *a, **kw: dev_range2
default_range_path = Path("gp200_34-A_to_34-D.zip")
if default_range_path.exists():
    default_range_path.unlink()
with contextlib.redirect_stdout(io.StringIO()):
    gp200.cmd_export(make_args(slot=None, start="34-A", end="34-D", out=None))
check("export --start/--end: derives a sensible default zip filename from the range "
      "when --out isn't given",
      default_range_path.exists())
if default_range_path.exists():
    default_range_path.unlink()

# --- mode validation: exactly one of {slot, --all, --start/--end} is required ---
exited_none = False
try:
    with contextlib.redirect_stdout(io.StringIO()):
        gp200.cmd_export(make_args(slot=None))
except SystemExit:
    exited_none = True
check("export: giving none of {slot, --all, --start/--end} is refused clearly",
      exited_none)

exited_both = False
try:
    with contextlib.redirect_stdout(io.StringIO()):
        gp200.cmd_export(make_args(slot="1-A", all=True))
except SystemExit:
    exited_both = True
check("export: giving BOTH a slot and --all is refused, not silently picking one",
      exited_both)

exited_half_range = False
message_half = ""
try:
    with contextlib.redirect_stdout(io.StringIO()):
        gp200.cmd_export(make_args(slot=None, start="34-A", end=None))
except SystemExit as e:
    exited_half_range = True
    message_half = str(e)
check("export: giving --start without --end is refused clearly",
      exited_half_range and "together" in message_half)

exited_backwards = False
try:
    with contextlib.redirect_stdout(io.StringIO()):
        gp200.cmd_export(make_args(slot=None, start="10-A", end="5-A"))
except ValueError:
    # parse_slot_range raises ValueError directly; main()'s top-level handler
    # (not exercised by calling cmd_export directly here) converts this to a
    # clean sys.exit for real CLI use -- same convention apply-template relies on.
    exited_backwards = True
check("export: --start after --end (a backwards range) is refused, not silently empty",
      exited_backwards)

gp200.Device = orig_device
gp200.resolve_skeleton_bytes = orig_resolve_skeleton
gp200.confirm_overwrite_file = orig_confirm_overwrite_file

print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("ALL EXPORT-CONFIRMED-READ CHECKS PASSED")
