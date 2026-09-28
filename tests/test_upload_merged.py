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

import os, tempfile
import atexit, shutil
_tmp = tempfile.mkdtemp(prefix="gp200_test_")
atexit.register(shutil.rmtree, _tmp, ignore_errors=True)
os.chdir(_tmp)  # isolate the .zip/.prst files this writes

skeleton = gp200.resolve_skeleton_bytes(None)


def make_prst(name_bytes: bytes) -> bytes:
    out = bytearray(skeleton)
    out[0x44:0x54] = (name_bytes + b"\x00" * 16)[:16]  # clear the whole 16-byte field first
    import struct
    struct.pack_into(">H", out, gp200.CHECKSUM_OFF, gp200.prst_checksum(out))
    return bytes(out)


# ---- _parse_leading_slot_label ----
check("_parse_leading_slot_label: recognizes this tool's own export naming",
      gp200._parse_leading_slot_label("37A_Template.prst") == gp200.label_to_slot("37A"))
check("_parse_leading_slot_label: recognizes a zero-ish single-digit bank too",
      gp200._parse_leading_slot_label("8A_Something.prst") == gp200.label_to_slot("8A"))
check("_parse_leading_slot_label: a generic filename with underscores returns None",
      gp200._parse_leading_slot_label("Friedman_BE100.prst") is None)
check("_parse_leading_slot_label: a filename with no underscore at all returns None",
      gp200._parse_leading_slot_label("SomePatch.prst") is None)
check("_parse_leading_slot_label: a numeric-looking but letterless prefix returns None",
      gp200._parse_leading_slot_label("65_Super_Reverb.prst") is None)
# Real-world naming bug fix (2026-09-27): a space-separated, dash-in-label
# filename (as produced e.g. by hand or by other tools/desktop apps) must be
# recognized just as well as this tool's own underscore convention. These
# are the exact filenames from a real user-supplied zip that silently fell
# back to alphabetical ordering before the fix.
check("_parse_leading_slot_label: recognizes a SPACE-separated, dash-in-label filename",
      gp200._parse_leading_slot_label("36-A JImi.prst") == gp200.label_to_slot("36-A"))
check("_parse_leading_slot_label: space-separated label works for multi-word patch names too",
      gp200._parse_leading_slot_label("34-A Hard Rock.prst") == gp200.label_to_slot("34-A"))
check("_parse_leading_slot_label: space-separated label works alongside a B/C/D letter",
      gp200._parse_leading_slot_label("34-B Lead.prst") == gp200.label_to_slot("34-B") and
      gp200._parse_leading_slot_label("34-C Special.prst") == gp200.label_to_slot("34-C"))
check("_parse_leading_slot_label: a space-separated generic filename still returns None",
      gp200._parse_leading_slot_label("Super Reverb.prst") is None)


# ---- expand_import_sources: plain file list (unchanged behavior) ----
p1, p2 = Path("a.prst"), Path("b.prst")
p1.write_bytes(make_prst(b"A"))
p2.write_bytes(make_prst(b"B"))
result = gp200.expand_import_sources(["b.prst", "a.prst"])
check("expand_import_sources: a plain file list keeps the given command-line order",
      [r[0] for r in result] == ["b.prst", "a.prst"])
check("expand_import_sources: a plain file list's bytes are read correctly",
      result[0][1] == make_prst(b"B") and result[1][1] == make_prst(b"A"))
p1.unlink(); p2.unlink()

# ---- expand_import_sources: zip with fully-labeled entries -> numeric slot order ----
zpath = Path("labeled.zip")
with zipfile.ZipFile(zpath, "w") as zf:
    zf.writestr("48D_Last.prst", make_prst(b"Last"))
    zf.writestr("8A_Early.prst", make_prst(b"Early"))     # would sort AFTER "48D" alphabetically
    zf.writestr("37A_Middle.prst", make_prst(b"Middle"))
out = io.StringIO()
with contextlib.redirect_stdout(out):
    result = gp200.expand_import_sources([str(zpath)])
check("expand_import_sources: fully-labeled zip entries sort by NUMERIC slot, not alphabetically",
      [r[0] for r in result] == ["8A_Early.prst", "37A_Middle.prst", "48D_Last.prst"])
check("expand_import_sources: says it used the embedded slot label",
      "ordered by their embedded slot label" in out.getvalue())
zpath.unlink()

# ---- expand_import_sources: zip using the real-world space/dash naming
#      convention (2026-09-27 regression: this exact shape used to fall back
#      to alphabetical silently) -> still sorts by NUMERIC slot ----
zpath_real = Path("real_naming.zip")
with zipfile.ZipFile(zpath_real, "w") as zf:
    zf.writestr("36-A JImi.prst", make_prst(b"JImi"))
    zf.writestr("35-A Mandolin.prst", make_prst(b"Mandolin"))
    zf.writestr("34-A Hard Rock.prst", make_prst(b"HardRock"))
    zf.writestr("34-B Lead.prst", make_prst(b"Lead"))
    zf.writestr("34-C Special.prst", make_prst(b"Special"))
out_real = io.StringIO()
with contextlib.redirect_stdout(out_real):
    result_real = gp200.expand_import_sources([str(zpath_real)])
check("expand_import_sources: real-world space/dash-labeled zip sorts by NUMERIC slot "
      "(34-A, 34-B, 34-C, 35-A, 36-A), not alphabetically",
      [r[0] for r in result_real] == [
          "34-A Hard Rock.prst", "34-B Lead.prst", "34-C Special.prst",
          "35-A Mandolin.prst", "36-A JImi.prst",
      ])
check("expand_import_sources: real-world naming case says it used the embedded slot label",
      "ordered by their embedded slot label" in out_real.getvalue())
zpath_real.unlink()

# ---- expand_import_sources: zip with unlabeled entries -> alphabetical fallback ----
zpath2 = Path("unlabeled.zip")
with zipfile.ZipFile(zpath2, "w") as zf:
    zf.writestr("JCM800_Recipe.prst", make_prst(b"JCM"))
    zf.writestr("65_Super_Reverb.prst", make_prst(b"65"))
    zf.writestr("Friedman_BE100.prst", make_prst(b"Fried"))
out2 = io.StringIO()
with contextlib.redirect_stdout(out2):
    result2 = gp200.expand_import_sources([str(zpath2)])
check("expand_import_sources: zip with no recognized labels falls back to alphabetical order",
      [r[0] for r in result2] == ["65_Super_Reverb.prst", "Friedman_BE100.prst", "JCM800_Recipe.prst"])
check("expand_import_sources: says it fell back to alphabetical order",
      "ordered alphabetically" in out2.getvalue())
zpath2.unlink()

# ---- expand_import_sources: MIXED labeled/unlabeled -> falls back to alphabetical (all-or-nothing) ----
zpath3 = Path("mixed.zip")
with zipfile.ZipFile(zpath3, "w") as zf:
    zf.writestr("50A_Named.prst", make_prst(b"Named"))
    zf.writestr("Unlabeled.prst", make_prst(b"Unlabeled"))
with contextlib.redirect_stdout(io.StringIO()):
    result3 = gp200.expand_import_sources([str(zpath3)])
check("expand_import_sources: a MIX of labeled/unlabeled entries falls back to alphabetical "
      "(all-or-nothing, not partially numeric)",
      [r[0] for r in result3] == ["50A_Named.prst", "Unlabeled.prst"])  # alphabetical: '5' < 'U'
zpath3.unlink()

# ---- expand_import_sources: non-.prst junk inside a zip is skipped, not imported ----
zpath4 = Path("withjunk.zip")
with zipfile.ZipFile(zpath4, "w") as zf:
    zf.writestr("1A_Good.prst", make_prst(b"Good"))
    zf.writestr("readme.txt", b"not a patch")
out4 = io.StringIO()
with contextlib.redirect_stdout(out4):
    result4 = gp200.expand_import_sources([str(zpath4)])
check("expand_import_sources: non-.prst zip entries are excluded from the result",
      [r[0] for r in result4] == ["1A_Good.prst"])
check("expand_import_sources: says it skipped the non-.prst entry",
      "skipping readme.txt" in out4.getvalue())
zpath4.unlink()

# ---- expand_import_sources: a zip with NO .prst entries at all is a clean error ----
zpath5 = Path("empty.zip")
with zipfile.ZipFile(zpath5, "w") as zf:
    zf.writestr("readme.txt", b"nothing useful")
exited = False
try:
    with contextlib.redirect_stdout(io.StringIO()):
        gp200.expand_import_sources([str(zpath5)])
except SystemExit:
    exited = True
check("expand_import_sources: a zip with no .prst files exits cleanly rather than importing nothing silently",
      exited)
zpath5.unlink()

# ---- expand_import_sources: mixing a zip with individual files is refused ----
p3 = Path("c.prst")
p3.write_bytes(make_prst(b"C"))
zpath6 = Path("solo.zip")
with zipfile.ZipFile(zpath6, "w") as zf:
    zf.writestr("1A_X.prst", make_prst(b"X"))
exited2 = False
message = ""
try:
    with contextlib.redirect_stdout(io.StringIO()):
        gp200.expand_import_sources([str(zpath6), "c.prst"])
except SystemExit as e:
    exited2 = True
    message = str(e)
check("expand_import_sources: mixing a .zip with individual files is refused, not silently merged",
      exited2 and "mix" in message.lower())
p3.unlink(); zpath6.unlink()


# ---- end-to-end: cmd_upload (merged upload/import, 2026-09-27) with a zip,
#      the trailing destination argument always wins over any embedded label ----
orig_device = gp200.Device
orig_resolve_verify_skeleton = gp200._resolve_verify_skeleton
orig_confirm_overwrite = gp200.confirm_overwrite
gp200._resolve_verify_skeleton = lambda: skeleton
gp200.confirm_overwrite = lambda dev, slot, force: True


def make_args(**overrides):
    ns = argparse.Namespace(targets=[], port=None, debug=False, force=True,
                             method="flash", commit=False)
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


class FakeDevImport:
    def __init__(self):
        self.writes = []  # (slot, name_written)
        self.closed = False
    def write_slot(self, slot, fb, currently_active, method=None, commit=False):
        pass
    def verify_write_full(self, slot, fb, skel):
        roundtrip = gp200.build_prst_from_dump(fb[gp200.CONTENT_FILE_START:gp200.CHECKSUM_OFF], "x", skel)
        self.writes.append((slot, gp200.prst_file_name(roundtrip)))
        return True, [], gp200.prst_file_name(roundtrip), roundtrip
    def close(self):
        self.closed = True


zpath7 = Path("e2e.zip")
with zipfile.ZipFile(zpath7, "w") as zf:
    # Deliberately labeled for slots far from where --start will actually put them --
    # this proves the label affects ORDER ONLY, never the destination.
    zf.writestr("48D_Gamma.prst", make_prst(b"Gamma"))
    zf.writestr("8A_Alpha.prst", make_prst(b"Alpha"))
    zf.writestr("37A_Beta.prst", make_prst(b"Beta"))

dev = FakeDevImport()
gp200.Device = lambda *a, **kw: dev
with contextlib.redirect_stdout(io.StringIO()):
    gp200.cmd_upload(make_args(targets=[str(zpath7), "20-B"]))

expected_start = gp200.label_to_slot("20-B")
check("cmd_upload + zip: writes go to the trailing destination argument regardless of "
      "embedded labels, in the label-sorted (Alpha, Beta, Gamma) order",
      dev.writes == [
          (expected_start, "Alpha"),
          (expected_start + 1, "Beta"),
          (expected_start + 2, "Gamma"),
      ])
zpath7.unlink()

# ---- cmd_upload: the old single-file/single-slot form (upload's original
#      shape) still works as the natural N=1 case ----
dev_single = FakeDevImport()
gp200.Device = lambda *a, **kw: dev_single
p4 = Path("single.prst")
p4.write_bytes(make_prst(b"Solo"))
with contextlib.redirect_stdout(io.StringIO()):
    gp200.cmd_upload(make_args(targets=["single.prst", "5-C"]))
check("cmd_upload: a single file + a single trailing slot still works (the old "
      "upload's exact shape, now just N=1 of the same operation)",
      dev_single.writes == [(gp200.label_to_slot("5-C"), "Solo")])
p4.unlink()

# ---- cmd_upload: fewer than 2 targets (no destination given) is a clear error ----
exited3 = False
try:
    with contextlib.redirect_stdout(io.StringIO()):
        gp200.cmd_upload(make_args(targets=["only_one.prst"]))
except SystemExit:
    exited3 = True
check("cmd_upload: a single positional argument (no destination slot) is refused clearly",
      exited3)

gp200.Device = orig_device
gp200._resolve_verify_skeleton = orig_resolve_verify_skeleton
gp200.confirm_overwrite = orig_confirm_overwrite

print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("ALL IMPORT-ZIP CHECKS PASSED")
