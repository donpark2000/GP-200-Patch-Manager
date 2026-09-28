"""When export --all/--start-end skips a slot (a real TimeoutError during a
batch read), the resulting zip has fewer entries than slots requested -- but
a zip has no concept of "this position is empty". `upload` later fills
slots consecutively from wherever ITS destination argument says, in zip
order, with no idea a slot was ever skipped (see upload's own help text).
So a gap here silently compacts on restore: every patch after the gap ends
up one slot earlier than where it actually came from.

This is exactly the kind of thing a musician doing a routine backup could
hit (an export --all over a real MIDI link occasionally times out on one
slot) and then not notice until a restore quietly shifts half their bank.
Covers that cmd_export prints an explicit warning naming which slot(s) were
skipped, right when it happens -- while the user still knows about it --
rather than staying silent and leaving it as a later mystery.
"""
import argparse, importlib.util, io, contextlib, zipfile
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

import os, tempfile, atexit, shutil
_tmp = tempfile.mkdtemp(prefix="gp200_test_")
atexit.register(shutil.rmtree, _tmp, ignore_errors=True)
os.chdir(_tmp)

skeleton = gp200.resolve_skeleton_bytes(None)
dump_clean = bytes(skeleton)[gp200.CONTENT_FILE_START:gp200.CHECKSUM_OFF]

orig_device = gp200.Device
orig_resolve_skeleton = gp200.resolve_skeleton_bytes
orig_confirm_overwrite_file = gp200.confirm_overwrite_file
gp200.resolve_skeleton_bytes = lambda p: skeleton
gp200.confirm_overwrite_file = lambda path, force: True


def make_args(**overrides):
    ns = argparse.Namespace(slot=None, out=None, all=False, start=None, end=None,
                             skeleton=None, force=True, port=None, debug=False)
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


# --- a range export where the MIDDLE slot times out: 34-A and 34-C succeed,
#     34-B doesn't. The zip should have 2 entries (not 3), and cmd_export
#     should print a clear warning naming 34-B specifically. ---
class FakeDevOneGap:
    def __init__(self):
        self.closed = False
    def read_dump_confirmed(self, slot, tries=3):
        if gp200.slot_to_label(slot) == "34B":  # slot_to_label has no dash (e.g. "34B", not "34-B")
            raise TimeoutError("no response")
        return dump_clean
    def close(self):
        self.closed = True


dev = FakeDevOneGap()
gp200.Device = lambda *a, **kw: dev
out_path = Path("gap_test.zip")
if out_path.exists():
    out_path.unlink()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    gp200.cmd_export(make_args(start="34-A", end="34-C", out=str(out_path)))
output = buf.getvalue()

check("export: the skipped slot is left out of the zip (2 entries, not 3)",
      out_path.exists() and len(zipfile.ZipFile(out_path).namelist()) == 2)
check("export: prints a WARNING when a slot was skipped",
      "WARNING" in output)
check("export: the warning names the SPECIFIC slot that was skipped (34B)",
      "34B" in output.split("WARNING")[-1])
check("export: the warning explains upload will NOT preserve the gap on restore",
      "gap" in output.lower() and "not preserved" in output.lower())
if out_path.exists():
    out_path.unlink()

# --- the clean/no-gap case must NOT print a spurious warning ---
class FakeDevNoGap:
    def __init__(self):
        self.closed = False
    def read_dump_confirmed(self, slot, tries=3):
        return dump_clean
    def close(self):
        self.closed = True


dev_clean = FakeDevNoGap()
gp200.Device = lambda *a, **kw: dev_clean
out_path2 = Path("no_gap_test.zip")
if out_path2.exists():
    out_path2.unlink()
buf2 = io.StringIO()
with contextlib.redirect_stdout(buf2):
    gp200.cmd_export(make_args(start="34-A", end="34-C", out=str(out_path2)))
check("export: no warning printed when nothing was skipped",
      "WARNING" not in buf2.getvalue())
if out_path2.exists():
    out_path2.unlink()

gp200.Device = orig_device
gp200.resolve_skeleton_bytes = orig_resolve_skeleton
gp200.confirm_overwrite_file = orig_confirm_overwrite_file

print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("ALL EXPORT GAP-WARNING CHECKS PASSED")
