import importlib.util, struct, time
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

# --- diff_prst_content: identical buffers -> no mismatches ---
check("diff_prst_content(x, x) is empty", gp200.diff_prst_content(skeleton, skeleton) == [])

# --- diff_prst_content ignores exactly the mapped benign offsets ---
mutated = bytearray(skeleton)
for off in list(gp200.VERIFY_IGNORE_OFFSETS)[:5]:
    mutated[off] ^= 0xFF
check("mutating only VERIFY_IGNORE_OFFSETS bytes still reports zero mismatches",
      gp200.diff_prst_content(skeleton, bytes(mutated)) == [])

# --- diff_prst_content DOES catch a real content change ---
mutated2 = bytearray(skeleton)
real_offset = 0xA0 + 0x0C  # block 0 param 0
mutated2[real_offset] ^= 0xFF
mismatches = gp200.diff_prst_content(skeleton, bytes(mutated2))
check("mutating a real content byte (block 0 param 0) is caught",
      len(mismatches) == 1 and mismatches[0][0] == real_offset)

# --- describe_prst_offset sanity ---
check("describe_prst_offset maps name region", gp200.describe_prst_offset(0x44) == "patch name")
check("describe_prst_offset maps author region", gp200.describe_prst_offset(0x54) == "author")
check("describe_prst_offset maps block/param", gp200.describe_prst_offset(0xA0 + 0x0C) == "block 0 param 0")
check("describe_prst_offset maps block/param (later block)",
      gp200.describe_prst_offset(0xA0 + 0x48*3 + 0x0C + 4*2) == "block 3 param 2")
check("describe_prst_offset maps effectId", gp200.describe_prst_offset(0xA0 + 8) == "block 0 effectId")
check("describe_prst_offset maps enabled", gp200.describe_prst_offset(0xA0 + 5) == "block 0 enabled")

# --- write_and_verify with a FakeDevice: succeeds on first try when clean ---
class FakeDeviceClean:
    def __init__(self, dump_bytes):
        self.dump_bytes = dump_bytes
        self.write_calls = 0
    def read_dump(self, slot):
        return self.dump_bytes
    def verify_write_full(self, slot, file_bytes, skeleton_bytes):
        roundtrip = gp200.build_prst_from_dump(self.dump_bytes, "x", skeleton_bytes)
        mismatches = gp200.diff_prst_content(file_bytes, roundtrip)
        return (len(mismatches) == 0), mismatches, gp200.prst_file_name(roundtrip), roundtrip

# monkeypatch do_write to a no-op so we don't need real MIDI I/O
calls = {"n": 0}
def fake_do_write(dev, slot, file_bytes, currently_active, method, commit):
    calls["n"] += 1
orig_do_write = gp200.do_write
gp200.do_write = fake_do_write

file_bytes = skeleton  # pretend we uploaded exactly the skeleton's own content
dump = file_bytes[gp200.CONTENT_FILE_START:gp200.CHECKSUM_OFF]
dev = FakeDeviceClean(dump)
ok, mismatches, name, attempts, _rt = gp200.write_and_verify(dev, 5, file_bytes, "flash", False, skeleton)
check("write_and_verify succeeds first try on a clean device", ok and attempts == 1 and calls["n"] == 1)

# --- write_and_verify retries on a transient failure, then succeeds ---
class FakeDeviceFlaky:
    def __init__(self, bad_dump, good_dump):
        self.bad_dump = bad_dump
        self.good_dump = good_dump
        self.attempt = 0
    def read_dump(self, slot):
        self.attempt += 1
        return self.bad_dump if self.attempt == 1 else self.good_dump
    def verify_write_full(self, slot, file_bytes, skeleton_bytes):
        dump = self.read_dump(slot) if False else None  # unused, real call goes through gp200's method below
        raise NotImplementedError

# Simpler: patch Device.verify_write_full-equivalent behavior directly via a
# small fake exposing verify_write_full itself (matching real call signature).
class FakeDeviceFlaky2:
    def __init__(self, bad_dump, good_dump):
        self.bad_dump = bad_dump
        self.good_dump = good_dump
        self.calls = 0
    def verify_write_full(self, slot, file_bytes, skeleton_bytes):
        self.calls += 1
        dump = self.bad_dump if self.calls == 1 else self.good_dump
        roundtrip = gp200.build_prst_from_dump(dump, "x", skeleton_bytes)
        mismatches = gp200.diff_prst_content(file_bytes, roundtrip)
        return (len(mismatches) == 0), mismatches, gp200.prst_file_name(roundtrip), roundtrip

bad_bytes = bytearray(skeleton)
bad_bytes[gp200.CONTENT_FILE_START + 100] ^= 0xFF  # corrupt something real
bad_dump = bytes(bad_bytes)[gp200.CONTENT_FILE_START:gp200.CHECKSUM_OFF]
good_dump = bytes(skeleton)[gp200.CONTENT_FILE_START:gp200.CHECKSUM_OFF]

calls["n"] = 0
dev2 = FakeDeviceFlaky2(bad_dump, good_dump)
ok2, mismatches2, name2, attempts2, _rt2 = gp200.write_and_verify(dev2, 5, file_bytes, "flash", False, skeleton)
check("write_and_verify retries once then succeeds on a flaky-then-clean device",
      ok2 and attempts2 == 2 and calls["n"] == 2)

# --- write_and_verify gives up after MAX_WRITE_ATTEMPTS on a persistently bad device ---
class FakeDeviceAlwaysBad:
    def verify_write_full(self, slot, file_bytes, skeleton_bytes):
        roundtrip = gp200.build_prst_from_dump(bad_dump, "x", skeleton_bytes)
        mismatches = gp200.diff_prst_content(file_bytes, roundtrip)
        return (len(mismatches) == 0), mismatches, gp200.prst_file_name(roundtrip), roundtrip

calls["n"] = 0
dev3 = FakeDeviceAlwaysBad()
ok3, mismatches3, name3, attempts3, _rt3 = gp200.write_and_verify(dev3, 5, file_bytes, "flash", False, skeleton)
check("write_and_verify gives up after MAX_WRITE_ATTEMPTS on a persistently bad device",
      not ok3 and attempts3 == gp200.MAX_WRITE_ATTEMPTS and calls["n"] == gp200.MAX_WRITE_ATTEMPTS
      and len(mismatches3) > 0)

gp200.do_write = orig_do_write

# --- DEAD_BYTE_FILE_OFFSETS (2026-09-29): a real write test showed the
# device won't ever store anything but 0x00 at file offset 0x43 or in the
# 0x8C-0xA0 range, regardless of what's sent -- see the constant's own
# docstring. diff_prst_content's default behavior (no extra_ignore) must
# still catch a mismatch there, because `reread` and soak-testing's
# _confirm_discrepancy rely on exactly that to detect and characterize
# read/write instability at this offset -- only verify_write_full's
# pass/fail judgment should be relaxed. ---
dead_byte_mutated = bytearray(skeleton)
dead_byte_mutated[0x43] ^= 0xFF
dead_byte_mutated[0x9F] ^= 0xFF
dead_byte_mutated = bytes(dead_byte_mutated)

check("diff_prst_content's DEFAULT behavior still catches a mismatch at the "
      "dead-byte offsets (reread/soak-test diagnostics must keep seeing this)",
      {m[0] for m in gp200.diff_prst_content(skeleton, dead_byte_mutated)} == {0x43, 0x9F})

check("diff_prst_content WITH extra_ignore=DEAD_BYTE_FILE_OFFSETS ignores a "
      "mismatch confined to exactly those offsets",
      gp200.diff_prst_content(skeleton, dead_byte_mutated,
                               extra_ignore=gp200.DEAD_BYTE_FILE_OFFSETS) == [])

# A mismatch elsewhere is NOT swallowed just because extra_ignore was passed --
# this isn't a blanket "ignore everything" escape hatch.
dead_plus_real = bytearray(dead_byte_mutated)
real_offset2 = 0xA0 + 0x0C  # block 0 param 0, same real-content byte used above
dead_plus_real[real_offset2] ^= 0xFF
mismatches_dead_plus_real = gp200.diff_prst_content(
    skeleton, bytes(dead_plus_real), extra_ignore=gp200.DEAD_BYTE_FILE_OFFSETS)
check("extra_ignore still catches a REAL content mismatch alongside the "
      "ignored dead bytes",
      len(mismatches_dead_plus_real) == 1 and mismatches_dead_plus_real[0][0] == real_offset2)

# --- The real Device.verify_write_full (not a fake standing in for it):
# a device that only ever disagrees at the dead-byte offsets must verify
# as OK on the very first attempt -- this is the actual scenario the user
# hit via `upload` (WRITE FAILED TO VERIFY, 10/10 identical mismatch at
# 0x43/0x9F only), which should no longer happen for that reason alone. ---
class FakeDeviceRealVerify:
    verify_write_full = gp200.Device.verify_write_full
    read_dump_confirmed = gp200.Device.read_dump_confirmed
    _dbg = gp200.Device._dbg

    def __init__(self, dump_bytes, debug=False):
        self.dump_bytes = dump_bytes
        self.debug = debug
        self._t0 = time.monotonic()

    def read_dump(self, slot):
        return self.dump_bytes  # same every time -> confirms on attempt 2, no delay


dead_byte_dump = dead_byte_mutated[gp200.CONTENT_FILE_START:gp200.CHECKSUM_OFF]
dev_dead_byte = FakeDeviceRealVerify(dead_byte_dump)
ok_db, mismatches_db, name_db, _rt_db = dev_dead_byte.verify_write_full(5, skeleton, skeleton)
check("Device.verify_write_full reports OK when the device only disagrees "
      "at the known dead-byte offsets (2026-09-29 fix: this used to be a "
      "false WRITE FAILED TO VERIFY)",
      ok_db and mismatches_db == [])

# A real mismatch elsewhere must still be caught by the real verify_write_full.
real_bad = bytearray(skeleton)
real_bad[0xA0 + 0x0C] ^= 0xFF  # block 0 param 0
real_bad_dump = bytes(real_bad)[gp200.CONTENT_FILE_START:gp200.CHECKSUM_OFF]
dev_real_bad = FakeDeviceRealVerify(real_bad_dump)
ok_rb, mismatches_rb, name_rb, _rt_rb = dev_real_bad.verify_write_full(5, skeleton, skeleton)
check("Device.verify_write_full still fails on a genuine content mismatch",
      not ok_rb and len(mismatches_rb) == 1 and mismatches_rb[0][0] == 0xA0 + 0x0C)

print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("ALL WRITE-AND-VERIFY CHECKS PASSED")
