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

# ---------------------------------------------------------------------------
# 1. Device.verify_write_full now returns a 4-tuple including the roundtrip
#    bytes, and write_and_verify propagates it as a 5th return value.
# ---------------------------------------------------------------------------
class FakeDeviceClean:
    def __init__(self, dump_bytes):
        self.dump_bytes = dump_bytes
    def read_dump(self, slot):
        return self.dump_bytes
    def read_dump_confirmed(self, slot, tries=3):
        # verify_write_full now reads via read_dump_confirmed, not a bare
        # read_dump -- this fake is always consistent, so just delegate.
        return gp200.Device.read_dump_confirmed(self, slot, tries)
    def verify_write_full(self, slot, file_bytes, skeleton_bytes):
        return gp200.Device.verify_write_full(self, slot, file_bytes, skeleton_bytes)

dev = FakeDeviceClean(dump_clean)
result = gp200.Device.verify_write_full(dev, 5, skeleton, skeleton)
check("verify_write_full returns a 4-tuple", len(result) == 4)
ok, mismatches, name, roundtrip = result
check("verify_write_full's 4th element is the actual roundtrip bytes",
      isinstance(roundtrip, (bytes, bytearray)) and len(roundtrip) == gp200.PRST_LEN)

orig_do_write = gp200.do_write
gp200.do_write = lambda *a, **kw: None
res = gp200.write_and_verify(dev, 5, skeleton, "flash", False, skeleton)
check("write_and_verify returns a 5-tuple", len(res) == 5)
check("write_and_verify's 5th element is the roundtrip bytes on success",
      isinstance(res[4], (bytes, bytearray)))
gp200.do_write = orig_do_write

# ---------------------------------------------------------------------------
# 2. print_failure_diagnostics: unconditional (no -d needed), full mismatch
#    list (not truncated to 6), environment info, and saves the failed
#    readback to disk.
# ---------------------------------------------------------------------------
import os, tempfile
import atexit, shutil
_tmp = tempfile.mkdtemp(prefix="gp200_test_")
atexit.register(shutil.rmtree, _tmp, ignore_errors=True)
os.chdir(_tmp)  # isolate the failed_verify_*.prst this writes
for f in Path(".").glob("failed_verify_*.prst"):
    f.unlink()

many_mismatches = [(gp200.CONTENT_FILE_START + i, 0x00, 0xFF) for i in range(10)]
out = io.StringIO()
with contextlib.redirect_stdout(out):
    gp200.print_failure_diagnostics(5, many_mismatches, "some name", "flash", False, 10, bytes(skeleton))
text = out.getvalue()
check("print_failure_diagnostics prints all 10 mismatches, not truncated to 6",
      text.count("expected 0x00") == 10 and "...and" not in text)
check("print_failure_diagnostics prints environment info (python version)",
      "python:" in text and "OS:" in text)
saved = list(Path(".").glob("failed_verify_*.prst"))
check("print_failure_diagnostics saved the failed readback to disk",
      len(saved) == 1 and saved[0].read_bytes() == bytes(skeleton))
for f in saved:
    f.unlink()

# no-mismatches / no-response case shouldn't crash and should say so
out2 = io.StringIO()
with contextlib.redirect_stdout(out2):
    gp200.print_failure_diagnostics(5, None, "(no response)", "flash", False, 10, None)
check("print_failure_diagnostics handles a no-response failure without crashing",
      "reading the slot back" in out2.getvalue() and "(no response)" in out2.getvalue())

# ---------------------------------------------------------------------------
# 3. cmd_reread: all-agree case, and a disagreement case (simulating a
#    read-side glitch with nothing ever written in between).
# ---------------------------------------------------------------------------
orig_device = gp200.Device
orig_read_bytes = gp200.Path.read_bytes
orig_resolve_verify_skeleton = gp200._resolve_verify_skeleton
gp200.Path.read_bytes = lambda self: bytes(skeleton)
gp200._resolve_verify_skeleton = lambda: skeleton

def make_reread_args(**overrides):
    ns = argparse.Namespace(slot="1-A", port=None, debug=False, count=5, delay=0.0, against=None)
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


class FakeDevReread:
    def __init__(self, dumps):
        self.dumps = list(dumps)
        self.closed = False
    def read_dump(self, slot):
        return self.dumps.pop(0)
    def close(self):
        self.closed = True

dev_agree = FakeDevReread([dump_clean] * 5)
gp200.Device = lambda *a, **kw: dev_agree
out3 = io.StringIO()
with contextlib.redirect_stdout(out3):
    gp200.cmd_reread(make_reread_args())
check("cmd_reread: 5 identical reads all report 'matches'", out3.getvalue().count("matches") == 5)
check("cmd_reread: agreeing reads conclude no read-side instability",
      "no evidence of read-side instability" in out3.getvalue())
check("cmd_reread: closes the device connection", dev_agree.closed)

# one glitchy read in the middle -- nothing was ever written, so this can
# only be explained by the read/receive side, not the device's flash.
dev_glitch = FakeDevReread([dump_clean, dump_clean, dump_bad, dump_clean, dump_clean])
gp200.Device = lambda *a, **kw: dev_glitch
out4 = io.StringIO()
with contextlib.redirect_stdout(out4):
    gp200.cmd_reread(make_reread_args())
text4 = out4.getvalue()
check("cmd_reread: a lone glitchy read among unwritten re-reads is flagged as DIFFERS",
      "DIFFERS" in text4)
check("cmd_reread: concludes it points at the host receive side, not the device",
      "host's receive side" in text4)

# --against a reference file
dev_ref = FakeDevReread([dump_bad] * 3)
gp200.Device = lambda *a, **kw: dev_ref
orig_path_init_read = gp200.Path.read_bytes
# --against reads a *different* file path than the one auto-read for the
# .prst validity check inside cmd_reread -- both go through Path.read_bytes,
# and we've stubbed that to always return `skeleton` (clean), so comparing
# against it while the device returns dump_bad should show a mismatch.
out5 = io.StringIO()
with contextlib.redirect_stdout(out5):
    gp200.cmd_reread(make_reread_args(against="reference.prst", count=3))
check("cmd_reread --against: compares reads against the given reference file, not the first read",
      "DIFFERS" in out5.getvalue())

gp200.Device = orig_device
gp200.Path.read_bytes = orig_read_bytes
gp200._resolve_verify_skeleton = orig_resolve_verify_skeleton

print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("ALL DIAGNOSTICS-AND-REREAD CHECKS PASSED")
