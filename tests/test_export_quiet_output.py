"""A real 256-slot `export --all` run (2026-09-29) produced a LOT of console
noise even though nothing was actually wrong: two lines per slot from
build_prst_from_dump's "(overlaid N of M dump bytes...)" accounting message
(256 lines total, every single one saying the same routine thing), plus 2-4
more lines from read_dump_confirmed's retry-diagnostic prints on every slot
that needed more than the trivial first-two-agree case (a meaningful
fraction of slots, given the independently-measured ~12-15%-per-read glitch
rate this project already knows about). Direct feedback: "I also saw a lot
of extra output that should probably be suppressed unless -d is used."

Both sources moved behind --debug (gp200.py: build_prst_from_dump gained a
`debug` parameter that every caller now threads through; read_dump_confirmed's
two print() calls became self._dbg() calls, which are gated on self.debug).

**Round 2 (same day, next real test run):** even with those two gone, there
was still more than a guitar player needs. Three more pieces of direct
feedback, all addressed here:
  - "I don't see much value in spitting out the name of every patch. The
    summary at the end should be enough." -> the per-slot `"{label}: {name!r}
    ({bytes} bytes)"` progress line (both export's single-slot and batch
    paths) is now ALSO --debug-only. The single-slot path's final "Wrote
    {out_path}" line already names the patch (it's baked into the filename),
    and the batch path already has its own end-of-run summary -- neither
    needs the noisier line repeated at every step.
  - "the message for a skipped one about 5 consecutive reads not matching
    is a debug thing - meaningless to a guitar player" -> a skipped/failed
    slot's message no longer embeds the raw ReadNotConfirmedError text
    ("N read(s) of X never agreed with each other..."), which names an
    internal retry count. describe_read_failure() gives a plain-language
    reason instead ("couldn't get a reliable read" / "no response from the
    device"); the raw detail is still appended when --debug is on.
  - "the reference to the PROTOCOL NOTES file can be dropped ... that is
    more for the benefit of anyone trying to write software" -> covered in
    test_ir_nam_dependency_warning.py (the NOTE message this applies to is
    that feature's), not duplicated here.

Unit-level coverage for the two Round 1 mechanisms separately lives in
test_gp200.py (build_prst_from_dump) and test_read_dump_confirmed.py
(read_dump_confirmed). This file covers the thing both bug reports were
actually about: running `export` end-to-end and checking the console output
a real user would actually see -- quiet without -d, and still fully
informative with it -- rather than trusting that each piece being
individually correct guarantees the combination is."""
import argparse, contextlib, importlib.util, io, time
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
dump_glitch1 = bytearray(dump_clean)
dump_glitch1[0x9F - gp200.CONTENT_FILE_START] = 0xB7  # a real, previously-seen corrupted value
dump_glitch1 = bytes(dump_glitch1)

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


class FakeDevMixedSlots:
    """Two slots: 1-A reads clean on the first try (the common case, no
    retry needed at all); 1-B's first read is glitched and needs a second,
    agreeing read to confirm (read_dump_confirmed's own real retry logic
    handles this -- read_dump_confirmed and _dbg are the REAL Device
    methods, reused here rather than faked out, so this test exercises the
    actual gating this fix is about, not a stand-in for it)."""
    read_dump_confirmed = gp200.Device.read_dump_confirmed
    _dbg = gp200.Device._dbg

    def __init__(self, debug=False):
        self.debug = debug
        self._t0 = time.monotonic()
        self.read_calls = {}
        self.closed = False

    def read_dump(self, slot):
        label = gp200.slot_to_label(slot)
        n = self.read_calls.get(label, 0) + 1
        self.read_calls[label] = n
        if label == "1B" and n == 1:
            return dump_glitch1
        return dump_clean

    def close(self):
        self.closed = True


# --- default (no --debug): must be QUIET about ALL the routine stuff --
#     including, as of round 2, the per-slot progress line itself -- but NOT
#     silent overall: the final summary line is still exactly what a user
#     relies on to see the export actually happened. ---
gp200.Device = lambda *a, **kw: FakeDevMixedSlots(debug=kw.get("debug", False))
out_path = Path("quiet.zip")
if out_path.exists():
    out_path.unlink()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    gp200.cmd_export(make_args(start="1-A", end="1-B", out=str(out_path), debug=False))
out = buf.getvalue()

check("export (no -d): the overlay accounting line is gone",
      "overlaid" not in out)
check("export (no -d): read_dump_confirmed's retry-diagnostic lines are gone, "
      "even though slot 1-B genuinely needed a retry to confirm",
      "read_dump_confirmed(" not in out)
check("export (no -d): the per-slot progress line (name + byte count) is ALSO gone now "
      "(round 2, 2026-09-29: \"I don't see much value in spitting out the name of every "
      "patch. The summary at the end should be enough.\")",
      "1A:" not in out and "1B:" not in out)
check("export (no -d): the final 'Wrote N patches...' summary is STILL there -- this is "
      "a noise fix, not a silence-everything regression",
      "Wrote 2 patches" in out)
if out_path.exists():
    out_path.unlink()


# --- --debug: the exact same run, same fake, same glitch -- ALL diagnostic
#     detail (overlay accounting, retry diagnostics, AND the per-slot
#     progress line) must still be fully available when asked for. ---
gp200.Device = lambda *a, **kw: FakeDevMixedSlots(debug=kw.get("debug", False))
out_path2 = Path("verbose.zip")
if out_path2.exists():
    out_path2.unlink()
buf2 = io.StringIO()
with contextlib.redirect_stdout(buf2):
    gp200.cmd_export(make_args(start="1-A", end="1-B", out=str(out_path2), debug=True))
out2 = buf2.getvalue()

check("export (-d): the overlay accounting line is back",
      "overlaid" in out2)
check("export (-d): read_dump_confirmed's retry-diagnostic line for the genuinely "
      "glitchy slot (1-B) is back",
      "read_dump_confirmed(1B)" in out2)
check("export (-d): the per-slot progress line is back too",
      "1A:" in out2 and "1B:" in out2)
if out_path2.exists():
    out_path2.unlink()


# --- a slot that never confirms at all (not just a glitch-then-recover):
#     the skip message must use plain language by default, not the raw
#     "N read(s) of X never agreed with each other" exception text -- and
#     still offer that raw detail when --debug is on. ---
def _dump_with_marker(value: int) -> bytes:
    buf = bytearray(dump_clean)
    buf[0x9F - gp200.CONTENT_FILE_START] = value  # same real-world offset as elsewhere in this suite
    return bytes(buf)


# 5 mutually-distinct dumps -- read_dump_confirmed's default tries=5 will
# exhaust its whole budget without any two ever agreeing with each other.
NEVER_AGREE_SEQUENCE = [_dump_with_marker(v) for v in (0x11, 0x22, 0x33, 0x44, 0x55)]


class FakeDevNeverAgrees:
    # orig_device (saved before any test above reassigned gp200.Device to a
    # lambda), not gp200.Device -- by this point in the file gp200.Device no
    # longer refers to the real class.
    read_dump_confirmed = orig_device.read_dump_confirmed
    _dbg = orig_device._dbg

    def __init__(self, debug=False):
        self.debug = debug
        self._t0 = time.monotonic()
        self.closed = False
        self.reads = list(NEVER_AGREE_SEQUENCE)

    def read_dump(self, slot):
        return self.reads.pop(0)

    def close(self):
        self.closed = True


gp200.Device = lambda *a, **kw: FakeDevNeverAgrees(debug=kw.get("debug", False))
out_path3 = Path("never_agrees.zip")
if out_path3.exists():
    out_path3.unlink()
buf3 = io.StringIO()
with contextlib.redirect_stdout(buf3):
    gp200.cmd_export(make_args(start="1-A", end="1-A", out=str(out_path3), debug=False))
out3 = buf3.getvalue()
check("export (no -d): a slot that never confirms is reported in plain language",
      "couldn't get a reliable read" in out3)
check("export (no -d): ...NOT with the raw internal retry-count wording "
      "(2026-09-29: \"meaningless to a guitar player\")",
      "never agreed" not in out3)
if out_path3.exists():
    out_path3.unlink()

gp200.Device = lambda *a, **kw: FakeDevNeverAgrees(debug=kw.get("debug", False))
out_path4 = Path("never_agrees_debug.zip")
if out_path4.exists():
    out_path4.unlink()
buf4 = io.StringIO()
with contextlib.redirect_stdout(buf4):
    gp200.cmd_export(make_args(start="1-A", end="1-A", out=str(out_path4), debug=True))
out4 = buf4.getvalue()
check("export (-d): the same failure STILL includes the raw retry-count detail "
      "when --debug is on",
      "never agreed" in out4)
if out_path4.exists():
    out_path4.unlink()
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
print("ALL EXPORT QUIET-OUTPUT CHECKS PASSED")
