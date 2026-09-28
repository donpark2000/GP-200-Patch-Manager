"""A patch name isn't guaranteed to be ASCII -- e.g. a Chinese-firmware
GP-200 might store non-ASCII bytes in the 16-byte name field. Two things
matter here, checked independently:

1. The bytes actually written into the exported .prst file must be the raw
   device bytes, untouched -- whatever they mean, a re-upload or a real
   editor should see exactly what the device sent, not something Claude's
   code guessed at.
2. The *display* name (console printout, and the filename export derives
   from it) must not crash and must not quietly present confident-looking
   garbage. Decoding as ASCII with "?" replacement is a deliberate, honest
   choice: the device's actual supported character set for names isn't
   confirmed (see PROTOCOL_NOTES.md), so "?" flags "something non-ASCII is
   here" rather than a Latin-1 mapping inventing plausible-looking letters
   for bytes that were never meant to be read that way.
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

# Build a dump whose name field (dump-relative offset 28:44, i.e. file
# offset 0x44) holds bytes that are NOT valid ASCII -- standing in for
# something like a UTF-8 or GB2312-encoded CJK name, without asserting
# which encoding a real Chinese-firmware unit would actually use (unknown,
# and out of scope for this test -- see PROTOCOL_NOTES.md).
dump = bytearray(skeleton)[gp200.CONTENT_FILE_START:gp200.CHECKSUM_OFF]
NON_ASCII_NAME_BYTES = bytes([0xE4, 0xBD, 0xA0, 0xE5, 0xA5, 0xBD]) + b"\x00" * 10
dump[28:44] = NON_ASCII_NAME_BYTES
dump = bytes(dump)

# --- extract_name_field() itself: no crash, ASCII-only with '?' for
#     anything out of range, matches what parse_preset_name would produce
#     for the same bytes. ---
name = gp200.extract_name_field(dump)
latin1_mojibake = "".join(chr(b) for b in NON_ASCII_NAME_BYTES if b)
check("extract_name_field: does not crash on non-ASCII name bytes",
      isinstance(name, str))
check("extract_name_field: uses Python's standard 'unknown character' marker (U+FFFD) "
      "for each non-ASCII byte, the same way str.decode('ascii', 'replace') always does "
      "-- an honest 'this wasn't ASCII' signal",
      "�" in name)
check("extract_name_field: does NOT map bytes 1:1 onto Latin-1 (which would silently "
      "invent plausible-looking-but-wrong letters for a non-ASCII name)",
      name != latin1_mojibake)

# --- full cmd_export path: single-slot export with this dump. Must not
#     crash, must produce a valid filename, and -- the important safety
#     property -- the bytes actually written to the .prst file at the name
#     field's offset must be the ORIGINAL raw bytes, not the ASCII-mangled
#     display string. ---
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


class FakeDevNonAsciiName:
    def __init__(self):
        self.closed = False
    def read_dump_confirmed(self, slot, tries=3):
        return dump
    def close(self):
        self.closed = True


dev = FakeDevNonAsciiName()
gp200.Device = lambda *a, **kw: dev
out_path = Path("1A_test.prst")
if out_path.exists():
    out_path.unlink()
crashed = False
try:
    with contextlib.redirect_stdout(io.StringIO()):
        gp200.cmd_export(make_args(out=str(out_path)))
except Exception:
    crashed = True
check("export: a non-ASCII name in the dump does not crash the single-slot export path",
      not crashed)
check("export: the file is still written despite the non-ASCII name",
      out_path.exists())
if out_path.exists():
    written = out_path.read_bytes()
    check("export: the RAW name bytes in the written .prst file are untouched "
          "(preserved exactly, not re-encoded from the mangled display string)",
          written[0x44:0x44 + len(NON_ASCII_NAME_BYTES)] == NON_ASCII_NAME_BYTES)
    out_path.unlink()

# --- same check for the --all / batch export path (separate call site,
#     historically used a DIFFERENT, more naive decoding than the
#     single-slot path -- see extract_name_field's docstring) ---
class FakeDevAllNonAscii:
    def __init__(self):
        self.closed = False
    def read_dump_confirmed(self, slot, tries=3):
        return dump
    def close(self):
        self.closed = True


dev_all = FakeDevAllNonAscii()
gp200.Device = lambda *a, **kw: dev_all
out_path_zip = Path("all_test.zip")
if out_path_zip.exists():
    out_path_zip.unlink()
crashed_all = False
try:
    with contextlib.redirect_stdout(io.StringIO()):
        # A single-slot range (--start/--end both "1-A") exercises the same
        # batch-export code path as --all without reading all 256 slots.
        gp200.cmd_export(make_args(slot=None, start="1-A", end="1-A", out=str(out_path_zip)))
except Exception:
    crashed_all = True
check("export --all/range: a non-ASCII name in the dump does not crash the batch export path",
      not crashed_all)
if out_path_zip.exists():
    with zipfile.ZipFile(out_path_zip) as zf:
        names = zf.namelist()
        check("export --all/range: exactly one entry was written for the one slot requested",
              len(names) == 1)
        if names:
            entry_bytes = zf.read(names[0])
            check("export --all/range: the RAW name bytes in the zipped .prst entry are untouched",
                  entry_bytes[0x44:0x44 + len(NON_ASCII_NAME_BYTES)] == NON_ASCII_NAME_BYTES)
    out_path_zip.unlink()

gp200.Device = orig_device
gp200.resolve_skeleton_bytes = orig_resolve_skeleton
gp200.confirm_overwrite_file = orig_confirm_overwrite_file

print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("ALL EXPORT NAME-ENCODING CHECKS PASSED")
