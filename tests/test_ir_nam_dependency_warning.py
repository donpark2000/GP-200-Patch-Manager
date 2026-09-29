"""export-time IR/NAM-dependency warning (PROTOCOL_NOTES.md, Finding 11,
2026-09-28/29; requested directly 2026-09-29 once the `list` speed fix was
confirmed: "What about that warning on export of a patch referencing IR's
or NAM").

A patch's 11 effect blocks each store a plain 4-byte model code. Most codes
mean an actual built-in sound, but two sub-ranges instead mean "whatever the
user currently has loaded into User-IR/SnapTone slot N" -- the patch itself
never carries that IR/NAM content (see find_ir_nam_dependencies). `export`
can't fix that limitation, but it can tell the person about it right when
it matters: while they still know which patch, and can still do something
about it, rather than discovering a sound difference after a factory reset
or a move to a new device.

Covers:
  - describe_ir_nam_dependency's three code ranges (User-IR, SnapTone-as-amp,
    SnapTone-as-drive) and the "ordinary effect, nothing to report" case.
  - find_ir_nam_dependencies scanning all 11 blocks of a real-shaped decoded
    dump, including that a short/malformed dump is skipped rather than
    raising (this is an informational note, not something correctness
    depends on).
  - cmd_export's single-slot path: prints a NOTE naming the dependency when
    present, prints nothing extra for an ordinary patch.
  - cmd_export's batch path (--start/--end): collects per-slot findings and
    prints ONE end-of-run summary naming each affected slot and what it
    depends on, and stays silent when nothing in the batch depends on
    anything.
"""
import argparse, importlib.util, io, contextlib, struct, zipfile
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


# ---- describe_ir_nam_dependency: the three "means a slot" ranges, plus the
#      "ordinary effect code" (None) case ----
check("describe_ir_nam_dependency: User-IR slot 0 (range start)",
      gp200.describe_ir_nam_dependency(0x0A100000) == "User-IR slot 0")
check("describe_ir_nam_dependency: User-IR slot 29 (range end, inclusive)",
      gp200.describe_ir_nam_dependency(0x0A10001D) == "User-IR slot 29")
check("describe_ir_nam_dependency: one past the User-IR range is NOT a dependency",
      gp200.describe_ir_nam_dependency(0x0A10001E) is None)
# The AMP-position and DST-position ranges address the same 5 physical
# capture slots but are NOT interchangeable -- a patch can use a given
# capture as its amp, its drive, or (in different blocks) both at once --
# so the description must say which position, not just which slot
# (2026-09-29, direct request: "our warning should clarify which").
check("describe_ir_nam_dependency: SnapTone slot 0 from the AMP-position range is tagged '(amp)'",
      gp200.describe_ir_nam_dependency(0x0F000000) == "SnapTone (NAM) slot 0 (amp)")
check("describe_ir_nam_dependency: SnapTone slot 4 (AMP-position range end), still '(amp)'",
      gp200.describe_ir_nam_dependency(0x0F000004) == "SnapTone (NAM) slot 4 (amp)")
check("describe_ir_nam_dependency: SnapTone slot 0 from the DST-position range is tagged '(dist)'",
      gp200.describe_ir_nam_dependency(0x0F000005) == "SnapTone (NAM) slot 0 (dist)")
check("describe_ir_nam_dependency: SnapTone slot 4 (DST-position range end), still '(dist)'",
      gp200.describe_ir_nam_dependency(0x0F000009) == "SnapTone (NAM) slot 4 (dist)")
check("describe_ir_nam_dependency: same slot number, different position, are NOT equal "
      "(the position tag must actually distinguish them, not just decorate)",
      gp200.describe_ir_nam_dependency(0x0F000002) != gp200.describe_ir_nam_dependency(0x0F000007))
check("describe_ir_nam_dependency: an ordinary built-in effect code is None",
      gp200.describe_ir_nam_dependency(0x00010002) is None)


# ---- find_ir_nam_dependencies: scan a real-shaped decoded dump ----
def make_decoded_with_effect_codes(codes: dict) -> bytes:
    """A minimal fake 'decoded' dump: 11 effect blocks at the real
    DUMP_EFFECT_BLOCK_START/_SIZE layout, each block's model-code field
    (offset DUMP_EFFECT_MODEL_OFFSET) zeroed except where `codes` (block
    index -> LE uint32 model code) says otherwise."""
    total = gp200.DUMP_EFFECT_BLOCK_START + gp200.DUMP_EFFECT_BLOCK_COUNT * gp200.DUMP_EFFECT_BLOCK_SIZE
    buf = bytearray(total)
    for i in range(gp200.DUMP_EFFECT_BLOCK_COUNT):
        base = gp200.DUMP_EFFECT_BLOCK_START + i * gp200.DUMP_EFFECT_BLOCK_SIZE
        code = codes.get(i, 0x00010002)  # an arbitrary ordinary effect code
        struct.pack_into("<I", buf, base + gp200.DUMP_EFFECT_MODEL_OFFSET, code)
    return bytes(buf)


clean_dump = make_decoded_with_effect_codes({})
check("find_ir_nam_dependencies: an all-ordinary-effects patch reports nothing",
      gp200.find_ir_nam_dependencies(clean_dump) == [])

# Directly exercises the "perfectly possible" scenario raised 2026-09-29: a
# single patch referencing a User-IR AND a SnapTone-as-amp AND a
# SnapTone-as-dist all at once -- including the same underlying NAM slot
# (1) used as BOTH the amp (block 3) and the dist (block 5) in one patch,
# which must show up as two distinct, correctly-tagged entries, not one.
mixed_dump = make_decoded_with_effect_codes({
    0: 0x0A100003,   # User-IR slot 3
    3: 0x0F000001,   # SnapTone slot 1, used as amp
    5: 0x0F000006,   # SnapTone slot 1, used as dist -- SAME slot, other position
    10: 0x0F000007,  # SnapTone slot 2, used as dist
})
found = gp200.find_ir_nam_dependencies(mixed_dump)
check("find_ir_nam_dependencies: finds ALL FOUR dependent blocks, in chain order, "
      "correctly distinguishing the same slot used as amp vs. dist",
      found == ["User-IR slot 3", "SnapTone (NAM) slot 1 (amp)",
                "SnapTone (NAM) slot 1 (dist)", "SnapTone (NAM) slot 2 (dist)"])

check("find_ir_nam_dependencies: a too-short/malformed dump is skipped, not an error",
      gp200.find_ir_nam_dependencies(b"\x00" * 4) == [])


# ---- cmd_export, single-slot path ----
skeleton = gp200.resolve_skeleton_bytes(None)
dump_clean_full = bytes(skeleton)[gp200.CONTENT_FILE_START:gp200.CHECKSUM_OFF]


def dump_with_dependency(base_dump: bytes, block_index: int, code: int) -> bytes:
    """Overlay one effect block's model code onto an otherwise-real skeleton
    dump, so build_prst_from_dump/normalize_export_dynamic_fields (which the
    single-slot export path also runs) see a realistic, full-length dump --
    not just the minimal synthetic buffer used for the unit checks above."""
    buf = bytearray(base_dump)
    base = gp200.DUMP_EFFECT_BLOCK_START + block_index * gp200.DUMP_EFFECT_BLOCK_SIZE
    struct.pack_into("<I", buf, base + gp200.DUMP_EFFECT_MODEL_OFFSET, code)
    return bytes(buf)


def dump_with_dependencies(base_dump: bytes, overlays: dict) -> bytes:
    """Like dump_with_dependency, but applies several block overlays at
    once -- for testing a single patch that references more than one
    User-IR/SnapTone slot simultaneously (a real, expected case per direct
    request 2026-09-29, not an edge case being humored)."""
    buf = bytearray(base_dump)
    for block_index, code in overlays.items():
        base = gp200.DUMP_EFFECT_BLOCK_START + block_index * gp200.DUMP_EFFECT_BLOCK_SIZE
        struct.pack_into("<I", buf, base + gp200.DUMP_EFFECT_MODEL_OFFSET, code)
    return bytes(buf)


# A single patch depending on a User-IR AND a SnapTone-as-amp AND a
# SnapTone-as-dist all at once -- exactly the scenario raised 2026-09-29
# ("it is perfectly possible that one patch uses both as well as an IR"),
# run end-to-end through the real single-slot export path this time, not
# just the unit-level find_ir_nam_dependencies check above.
dump_dependent = dump_with_dependencies(dump_clean_full, {
    2: 0x0A100005,   # User-IR slot 5
    4: 0x0F000001,   # SnapTone slot 1, used as amp
    6: 0x0F000006,   # SnapTone slot 1, used as dist -- same slot, other position
})

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


class FakeDevSingle:
    def __init__(self, dump):
        self.dump = dump
        self.closed = False
    def read_dump_confirmed(self, slot, tries=3):
        return self.dump
    def close(self):
        self.closed = True


gp200.Device = lambda *a, **kw: FakeDevSingle(dump_dependent)
out_path = Path("single_dependent.prst")
if out_path.exists():
    out_path.unlink()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    gp200.cmd_export(make_args(slot="34-A", out=str(out_path)))
out = buf.getvalue()
check("export (single slot): prints a NOTE when the patch depends on a User-IR/SnapTone slot",
      "NOTE" in out and "User-IR slot 5" in out)
# Split on "NOTE:" (with the colon) -- the message itself mentions
# "PROTOCOL_NOTES.md", which contains "NOTE" as a substring and would
# otherwise make a bare split("NOTE") cut the tail off mid-sentence.
note_line = out.split("NOTE:")[-1]
check("export (single slot): the SAME NOTE also names the SnapTone-as-amp dependency "
      "(a patch can depend on more than one slot at once -- all must be mentioned)",
      "SnapTone (NAM) slot 1 (amp)" in note_line)
check("export (single slot): and the SnapTone-as-dist dependency on the SAME slot number, "
      "correctly distinguished from the amp one",
      "SnapTone (NAM) slot 1 (dist)" in note_line)
if out_path.exists():
    out_path.unlink()

gp200.Device = lambda *a, **kw: FakeDevSingle(dump_clean_full)
out_path2 = Path("single_clean.prst")
if out_path2.exists():
    out_path2.unlink()
buf2 = io.StringIO()
with contextlib.redirect_stdout(buf2):
    gp200.cmd_export(make_args(slot="34-A", out=str(out_path2)))
check("export (single slot): no NOTE printed for a patch with no such dependency",
      "NOTE" not in buf2.getvalue())
if out_path2.exists():
    out_path2.unlink()


# ---- cmd_export, batch (--start/--end) path ----
class FakeDevBatch:
    """34-A depends on a User-IR slot, 34-B and 34-C are ordinary patches."""
    def __init__(self):
        self.closed = False
    def read_dump_confirmed(self, slot, tries=3):
        if gp200.slot_to_label(slot) == "34A":
            return dump_with_dependency(dump_clean_full, 7, 0x0F000002)
        return dump_clean_full
    def close(self):
        self.closed = True


gp200.Device = lambda *a, **kw: FakeDevBatch()
out_path3 = Path("batch_dependent.zip")
if out_path3.exists():
    out_path3.unlink()
buf3 = io.StringIO()
with contextlib.redirect_stdout(buf3):
    gp200.cmd_export(make_args(start="34-A", end="34-C", out=str(out_path3)))
out3 = buf3.getvalue()
check("export (batch): the zip still gets all 3 entries (a dependency isn't a skip)",
      out_path3.exists() and len(zipfile.ZipFile(out_path3).namelist()) == 3)
check("export (batch): prints ONE end-of-run NOTE summarizing the dependent patch(es)",
      "NOTE:" in out3 and "1 patch(es)" in out3)
# Split on "NOTE:" (with the colon), not bare "NOTE" -- the summary itself
# points to "PROTOCOL_NOTES.md", which contains "NOTE" as a substring and
# would otherwise make split("NOTE") cut the tail off mid-sentence, before
# the very slot/dependency text these checks are looking for.
note_tail = out3.split("NOTE:")[-1]
check("export (batch): the summary names the SPECIFIC affected slot (34A)",
      "34A" in note_tail)
check("export (batch): the summary names what it depends on, including WHICH position "
      "(amp vs. dist) the SnapTone slot is used in",
      "SnapTone (NAM) slot 2 (amp)" in note_tail)
check("export (batch): the summary does not fault the clean slots (34B/34C absent from it)",
      "34B" not in note_tail and "34C" not in note_tail)
if out_path3.exists():
    out_path3.unlink()


class FakeDevBatchClean:
    def __init__(self):
        self.closed = False
    def read_dump_confirmed(self, slot, tries=3):
        return dump_clean_full
    def close(self):
        self.closed = True


gp200.Device = lambda *a, **kw: FakeDevBatchClean()
out_path4 = Path("batch_clean.zip")
if out_path4.exists():
    out_path4.unlink()
buf4 = io.StringIO()
with contextlib.redirect_stdout(buf4):
    gp200.cmd_export(make_args(start="34-A", end="34-C", out=str(out_path4)))
check("export (batch): no NOTE printed when nothing in the batch depends on anything",
      "NOTE" not in buf4.getvalue())
if out_path4.exists():
    out_path4.unlink()

gp200.Device = orig_device
gp200.resolve_skeleton_bytes = orig_resolve_skeleton
gp200.confirm_overwrite_file = orig_confirm_overwrite_file


print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("ALL IR/NAM DEPENDENCY WARNING CHECKS PASSED")
