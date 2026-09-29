import importlib.util, io, contextlib, time
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


class FakeDevSequence:
    """A bare object standing in for Device, with just enough to exercise
    read_dump_confirmed (an unbound method called as gp200.Device.read_dump_confirmed(self, ...)).

    `debug` defaults to False (matching real Device) and `_t0` is set so
    the reused `_dbg` method below works -- as of 2026-09-29,
    read_dump_confirmed's retry-diagnostic lines are gated behind
    `self.debug` (previously unconditional; see the method's docstring),
    so any stand-in for Device needs both attributes now, not just real
    Device instances."""
    _dbg = gp200.Device._dbg  # reuse the real implementation, not a copy of its logic

    def __init__(self, dumps, debug=False):
        self.dumps = list(dumps)
        self.calls = 0
        self.debug = debug
        self._t0 = time.monotonic()
    def read_dump(self, slot):
        self.calls += 1
        return self.dumps.pop(0)


GOOD = b"\x01" * 10
BAD = b"\x02" * 10
OTHER = b"\x03" * 10

# --- immediate agreement: first two reads already match, returns right away ---
dev1 = FakeDevSequence([GOOD, GOOD])
result1 = gp200.Device.read_dump_confirmed(dev1, 5)
check("read_dump_confirmed: two immediately-agreeing reads return that value",
      result1 == GOOD)
check("read_dump_confirmed: stops as soon as two reads agree (only 2 calls)",
      dev1.calls == 2)

# --- disagreement then agreement (consecutive): recovers once two in a row match ---
dev2 = FakeDevSequence([BAD, GOOD, GOOD])
result2 = gp200.Device.read_dump_confirmed(dev2, 5, tries=3)
check("read_dump_confirmed: recovers to the value two consecutive reads agree on",
      result2 == GOOD)
check("read_dump_confirmed: took exactly 3 reads to reach agreement", dev2.calls == 3)

# --- non-consecutive agreement: [A, B, A] -- the 1st and 3rd reads agree even
#     though nothing ADJACENT does. This is the exact case an earlier,
#     consecutive-only version of this method got wrong (it would return
#     whatever the last read happened to be, coincidentally or not, rather
#     than actually detecting that A recurred). The new version checks every
#     prior read, not just the immediately preceding one, so it must catch
#     this and return A having made only 3 calls (no 4th read needed).
dev3 = FakeDevSequence([GOOD, BAD, GOOD])
result3 = gp200.Device.read_dump_confirmed(dev3, 5, tries=3)
check("read_dump_confirmed: catches non-consecutive agreement (1st and 3rd reads matching)",
      result3 == GOOD)
check("read_dump_confirmed: non-consecutive agreement needs no more reads than consecutive would",
      dev3.calls == 3)

# --- never agrees within the tries budget: raises ReadNotConfirmedError
#     rather than silently returning an arbitrary, unconfirmed last read.
#     This is the key behavior change from the old implementation, which
#     used to just return its last attempt with no way for the caller to
#     tell an unconfirmed guess apart from a real, confirmed read. ---
dev4 = FakeDevSequence([GOOD, BAD, OTHER])
raised = None
try:
    gp200.Device.read_dump_confirmed(dev4, 5, tries=3)
except gp200.ReadNotConfirmedError as e:
    raised = e
check("read_dump_confirmed: raises ReadNotConfirmedError when reads never agree, doesn't hang/crash",
      raised is not None)
check("read_dump_confirmed: ReadNotConfirmedError is also a TimeoutError (existing catch sites still work)",
      raised is not None and isinstance(raised, TimeoutError))
check("read_dump_confirmed: makes exactly `tries` calls when it never agrees, not more",
      dev4.calls == 3)

# --- tries=2 (the minimum): behaves like the simple two-read case ---
dev5 = FakeDevSequence([BAD, BAD])
result5 = gp200.Device.read_dump_confirmed(dev5, 5, tries=2)
check("read_dump_confirmed: tries=2 with two agreeing reads returns that value",
      result5 == BAD and dev5.calls == 2)

dev6 = FakeDevSequence([GOOD, BAD])
raised6 = None
try:
    gp200.Device.read_dump_confirmed(dev6, 5, tries=2)
except gp200.ReadNotConfirmedError:
    raised6 = True
check("read_dump_confirmed: tries=2 with two disagreeing reads raises, not silently returns one",
      raised6 is True)

# --- default tries is now 5, not 3 (2026-09-27, PROTOCOL_NOTES.md finding
#     11) -- a real 256-slot export run showed 3-tries has a measurable
#     (~3.5%) "never agreed" rate at scale. Prove the new default actually
#     gives the extra headroom: 4 mutually-distinct reads followed by a 5th
#     that finally matches the FIRST one (non-consecutive, deliberately) --
#     old default=3 would have exhausted its budget and raised after read 3;
#     the new default=5 must keep going and confirm on read 5. Called with NO
#     `tries` argument at all, so this only passes if the default truly
#     changed. ---
BAD2, BAD3, BAD4 = b"\x04" * 10, b"\x05" * 10, b"\x06" * 10
dev_default5 = FakeDevSequence([GOOD, BAD, BAD2, BAD3, GOOD])
result_default5 = gp200.Device.read_dump_confirmed(dev_default5, 5)
check("read_dump_confirmed: default tries is now 5, not 3 -- confirms on the 5th "
      "read matching the 1st, a case the old default=3 would have missed entirely",
      result_default5 == GOOD and dev_default5.calls == 5)

# --- and the flip side: with the new default, 5 mutually-distinct reads
#     (never any two agreeing) still correctly raises rather than looping
#     forever or silently returning something ---
dev_default5_fail = FakeDevSequence([GOOD, BAD, BAD2, BAD3, BAD4])
raised_default5 = None
try:
    gp200.Device.read_dump_confirmed(dev_default5_fail, 5)
except gp200.ReadNotConfirmedError:
    raised_default5 = True
check("read_dump_confirmed: default tries=5 still raises cleanly when nothing ever "
      "agrees across all 5 attempts",
      raised_default5 is True and dev_default5_fail.calls == 5)

# --- tries < 2 is rejected outright -- confirming anything needs at least 2 reads ---
rejected = False
try:
    gp200.Device.read_dump_confirmed(FakeDevSequence([GOOD]), 5, tries=1)
except ValueError:
    rejected = True
check("read_dump_confirmed: tries=1 (or less) is rejected as a usage error",
      rejected)

# --- diagnostic printing: on any disagreement (whether it's eventually
#     resolved or not), read_dump_confirmed should show exactly what
#     differed between the reads, in file-offset terms, not fail silently
#     or force someone to guess what happened -- but, as of 2026-09-29, only
#     when --debug is on. This used to be unconditional; a real `export
#     --all` run showed that printed ~12-15%-per-read glitch noise on a
#     meaningful fraction of the 256 slots even when nothing was actually
#     wrong, so it moved behind self.debug like every other diagnostic
#     (direct feedback: "a lot of extra output that should probably be
#     suppressed unless -d is used"). Both sides of that are covered below:
#     silent by default, and still fully available with debug=True. ---
import struct
skeleton = gp200.resolve_skeleton_bytes(None)
real_a = bytearray(skeleton)[gp200.CONTENT_FILE_START:gp200.CHECKSUM_OFF]
real_b = bytearray(real_a)
real_b[0x9F - gp200.CONTENT_FILE_START] = 0xB7  # a real, previously-seen corrupted value
real_a, real_b = bytes(real_a), bytes(real_b)

# Default (debug=False): a diff-and-recover must stay QUIET on the console --
# this is the actual behavior change being tested, not just a side effect.
dev7_quiet = FakeDevSequence([real_a, real_b, real_a])  # 1st/3rd agree, 2nd differs
out_quiet = io.StringIO()
with contextlib.redirect_stdout(out_quiet):
    result7_quiet = gp200.Device.read_dump_confirmed(dev7_quiet, 5, tries=3)
check("read_dump_confirmed: still returns the right value when it needs a diff-and-recover "
      "(debug off)",
      result7_quiet == real_a)
check("read_dump_confirmed: prints NOTHING on a diff-and-recover when --debug is off "
      "(2026-09-29: this used to print unconditionally -- now it shouldn't)",
      out_quiet.getvalue() == "")

# debug=True: the same scenario, diagnostic detail must still be fully there.
dev7 = FakeDevSequence([real_a, real_b, real_a], debug=True)  # 1st/3rd agree, 2nd differs
out = io.StringIO()
with contextlib.redirect_stdout(out):
    result7 = gp200.Device.read_dump_confirmed(dev7, 5, tries=3)
text7 = out.getvalue()
check("read_dump_confirmed: still returns the right value when it needs a diff-and-recover "
      "(debug on)",
      result7 == real_a)
check("read_dump_confirmed: prints something when reads disagree AND --debug is on",
      len(text7.strip()) > 0)
check("read_dump_confirmed: the diagnostic names the actual byte offset that differed (0x9F)",
      "0x009F" in text7)
check("read_dump_confirmed: the diagnostic shows the actual differing values (0x00 vs 0xB7)",
      "0x00" in text7 and "0xB7" in text7)

# --- length-mismatch case: _describe_raw_dump_diff should say so plainly
#     rather than crashing on a zip() length mismatch or silently ignoring it ---
short = real_a[:-1]
diff_desc = gp200._describe_raw_dump_diff(real_a, short)
check("_describe_raw_dump_diff: reports a length mismatch explicitly instead of crashing",
      "LENGTH" in diff_desc)

# ---------------------------------------------------------------------------
# THE ACTUAL BUG FOUND ON REAL HARDWARE (2026-09-27): two reads of a
# completely untouched slot that differ ONLY in the tail block (which
# changes on every plain read, not just every save) must be treated as
# AGREEMENT, not disagreement -- comparing raw bytes with no filtering made
# every single read_dump_confirmed call fail 100% of the time on real
# hardware, even with zero writes anywhere in the session.
# ---------------------------------------------------------------------------
# Build two dumps that are IDENTICAL everywhere except the tail block
# (offsets 1120+q*12+6 and +7 for q in 0..7, in FILE-offset terms).
tail_a = bytearray(real_a)
tail_b = bytearray(real_a)
for q in range(8):
    off = (1120 + q * 12 + 6) - gp200.CONTENT_FILE_START
    tail_b[off] ^= 0xFF
    tail_b[off + 1] ^= 0xFF

check("raw_dumps_agree: two dumps differing ONLY in the tail block still agree",
      gp200.raw_dumps_agree(bytes(tail_a), bytes(tail_b)))

dev_tail = FakeDevSequence([bytes(tail_a), bytes(tail_b)])
out_tail = io.StringIO()
with contextlib.redirect_stdout(out_tail):
    result_tail = gp200.Device.read_dump_confirmed(dev_tail, 5, tries=2)
check("read_dump_confirmed: confirms successfully when only the tail block differs "
      "(the exact real-hardware failure this fixes)",
      result_tail is not None and dev_tail.calls == 2)

# And the converse: if something OUTSIDE the tail block also differs, that
# must still be caught -- the fix must not become "ignore everything".
tail_b_and_real_diff = bytearray(tail_b)
tail_b_and_real_diff[0x9F - gp200.CONTENT_FILE_START] = 0xB7
dev_tail_and_real = FakeDevSequence([bytes(tail_a), bytes(tail_b_and_real_diff)])
raised_tail = None
try:
    gp200.Device.read_dump_confirmed(dev_tail_and_real, 5, tries=2)
except gp200.ReadNotConfirmedError:
    raised_tail = True
check("raw_dumps_agree: still correctly detects a REAL difference alongside tail-block noise",
      raised_tail is True)

print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("ALL READ_DUMP_CONFIRMED CHECKS PASSED")
