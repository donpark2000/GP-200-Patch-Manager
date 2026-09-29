import argparse, importlib.util, io, contextlib, os, tempfile, time
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

import atexit, shutil
_tmp = tempfile.mkdtemp(prefix="gp200_test_")
atexit.register(shutil.rmtree, _tmp, ignore_errors=True)
os.chdir(_tmp)  # isolate any files this test writes

skeleton = gp200.resolve_skeleton_bytes(None)
dump_clean = bytes(skeleton)[gp200.CONTENT_FILE_START:gp200.CHECKSUM_OFF]
# Block 0 param 0 (0xA0 + 0x0C) -- not 0x28 (falls inside the
# intentionally-ignored device-owned range, VERIFY_IGNORE_OFFSETS /
# RAW_DUMP_IGNORE_OFFSETS) and, as of 2026-09-29, not 0x43/0x9F either
# (DEAD_BYTE_FILE_OFFSETS -- raw_dumps_agree now correctly ignores a
# difference confined to those two as well, since a real write test showed
# the device enforces 0x00 there regardless of what's sent). This picks an
# ordinary content offset so these fixtures actually differ somewhere
# raw_dumps_agree still treats as a real difference.
REAL_DIFF_OFFSET = 0xA0 + 0x0C
dump_a = bytearray(skeleton)
dump_a[REAL_DIFF_OFFSET] = 0x11
dump_a = bytes(dump_a)[gp200.CONTENT_FILE_START:gp200.CHECKSUM_OFF]
dump_b = bytearray(skeleton)
dump_b[REAL_DIFF_OFFSET] = 0x22
dump_b = bytes(dump_b)[gp200.CONTENT_FILE_START:gp200.CHECKSUM_OFF]

template_path = Path("diag_template.prst")
template_path.write_bytes(skeleton)

orig_device = gp200.Device
orig_resolve_skeleton = gp200.resolve_skeleton_bytes
gp200.resolve_skeleton_bytes = lambda p: skeleton


def make_args(**overrides):
    ns = argparse.Namespace(slot="34-B", template=str(template_path), skeleton=None,
                             backup_out="diag_backup_test.prst", port=None, debug=False)
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


class FakeDevDiag:
    # read_dump_confirmed below calls the REAL Device.read_dump_confirmed,
    # which as of 2026-09-29 gates its retry-diagnostic prints behind
    # self.debug (previously unconditional -- see PROTOCOL_NOTES.md) via
    # self._dbg(); both attributes are needed for that real method to run
    # against this fake at all now, not just against a real Device.
    debug = False
    _dbg = gp200.Device._dbg

    def __init__(self, backup_reads):
        self.backup_reads = list(backup_reads)
        self.write_calls = 0
        self.closed = False
        self._t0 = time.monotonic()
    def read_dump(self, slot):
        return self.backup_reads.pop(0)
    def read_dump_confirmed(self, slot, tries=3):
        return orig_device.read_dump_confirmed(self, slot, tries)
    def write_slot(self, slot, fb, currently_active):
        self.write_calls += 1
    def read_name(self, slot):
        return "some name"
    def close(self):
        self.closed = True


# --- backup read never agrees: abort BEFORE writing, don't crash ---
backup_out = Path("diag_backup_test.prst")
if backup_out.exists():
    backup_out.unlink()
dev1 = FakeDevDiag([dump_a, dump_b, dump_clean])  # 3 distinct reads, never agree
gp200.Device = lambda *a, **kw: dev1
exited = False
message = ""
try:
    with contextlib.redirect_stdout(io.StringIO()):
        gp200.cmd_diag_write(make_args())
except SystemExit as e:
    exited = True
    message = str(e)
check("diag-write: an unconfirmable backup read aborts with SystemExit, not a crash", exited)
check("diag-write: the abort message says it's aborting rather than writing unsafely",
      "aborting" in message.lower())
check("diag-write: the write was never attempted when the backup couldn't be confirmed",
      dev1.write_calls == 0)
check("diag-write: no backup file was left behind on this failure path",
      not backup_out.exists())
check("diag-write: device connection still closed on this failure path", dev1.closed)

# --- normal path: backup confirms fine, write proceeds, backup file saved ---
if backup_out.exists():
    backup_out.unlink()
dev2 = FakeDevDiag([dump_clean, dump_clean])  # agrees immediately
gp200.Device = lambda *a, **kw: dev2
with contextlib.redirect_stdout(io.StringIO()):
    gp200.cmd_diag_write(make_args())
check("diag-write: normal path (confirmed backup) proceeds to write exactly once",
      dev2.write_calls == 1)
check("diag-write: normal path saves the backup file",
      backup_out.exists())
if backup_out.exists():
    backup_out.unlink()

gp200.Device = orig_device
gp200.resolve_skeleton_bytes = orig_resolve_skeleton
template_path.unlink()

print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("ALL DIAG-WRITE CHECKS PASSED")
