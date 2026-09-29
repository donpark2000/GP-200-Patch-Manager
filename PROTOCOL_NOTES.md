# GP-200 SysEx protocol — working notes toward the eventual manual

Status: DRAFT / running notes, not the manual itself. Testing isn't finished
yet (per explicit decision to finish testing before writing this up
properly). This file exists so findings survive conversation compaction or a
new chat, even before the real document gets written. Update this file as
new things are confirmed, rather than trusting it to conversation memory.

## Shape of the eventual document

Three components, at three different levels of stability:

1. **Patch structure** — the `.prst` file format itself. Stable, verified
   against real hardware and against Valeton's own exports.
2. **Messaging and commands** — the SysEx protocol: handshake, message
   formats, encodings, addressing. Stable, verified against real hardware.
3. **Reliability** — why a protocol-correct write doesn't always stick, and
   what compensates for that. Still actively being fleshed out; this is
   where new findings currently land.

Respects RigSheet's license throughout: only protocol *facts* (byte
layouts, addressing) are recorded here, reverse-engineered independently in
our own Python, never RigSheet's own code/text.

## 1. Patch structure (`.prst` file format)

- 1224 bytes total. Magic `TSRP` @0x00.
- Name @0x44 (16 bytes); author @0x54 (16 bytes); note @0x64 (40 bytes).
- 11 effect blocks of 72 bytes each, starting at 0xA0:
  slotIndex @+4, enabled @+5, effectId (u32LE) @+8, 15x float32LE params
  from @+0x0C.
- Checksum = `sum(bytes[0:0x4C6]) & 0xFFFF`, big-endian, stored @0x4C6.
- Device-owned / benign bytes — not real patch content, so excluded from
  write verification:
  - 0x28–0x2D: PC-software-only version/checksum text stamp. Our upload
    never transmits this; only Valeton's own desktop exporter writes it.
    Export leaves it as whatever the skeleton file already has there.
  - 0x2E, 0x34, 0x90: slot-mirror bytes the device recomputes to reflect
    the actual target slot. Of the three, only 0x2E is also zeroed by
    Valeton's official exporter (confirmed via two independent real
    exports, 2026-09-27) — 0x34 and 0x90 come through as the real slot
    value in Valeton's own exports too. Our `export` now zeroes 0x2E to
    match (see Reliability, finding 9); 0x34/0x90 are left as-is, matching
    what Valeton actually does. This is an export-side-only finding — it
    doesn't establish what the device does with 0x2E on write; see the open
    question flagged in `build_upload_image`'s docstring.
  - Tail block at file offset 1120, 8 entries × 12-byte stride, meaningful
    bytes at `1120+q*12+6` and `+7` for q in 0..7: changes on every save
    regardless of content. RigSheet's own write-verification independently
    excludes this same region. As of 2026-09-27, our own `export` also
    zeroes this region in the file it writes (see Reliability, finding 9) —
    so it no longer appears in exported files at all, matching what we've
    observed of Valeton's own official exports.
- 0x8C–0x9F (20-byte gap between "note" and the first effect block):
  purpose still unknown, but confirmed 0x00 in every real sample seen this
  session (official Valeton exports and our own round-trip exports alike).
  Unlike the device-owned bytes above, this one is NOT supposed to vary —
  see Reliability, finding 2: this is the exact byte a real corruption run
  landed on, every time.
- Export/read always needs a real `.prst` "skeleton" to reconstruct a full
  file: a device dump doesn't include the file's own leading ~0x28 bytes,
  and can end a couple bytes short of the file's tail. This is normal, not
  a bug — needed on every export, not just template operations.
- 0x1C–0x1F (inside the pre-0x28 header, i.e. never covered by a device
  dump): confirmed genuinely dynamic per-export, not a fixed value we could
  copy into our skeleton to match Valeton more closely. Two independent
  Valeton Desktop exports of the exact same untouched patch (2026-09-27)
  differed ONLY here (and in the trailing checksum) — everything else,
  including the 0x28–0x2D stamp, was identical between the two. Purpose
  unknown (a per-export nonce, timestamp, or counter of some kind); not
  investigated further since it's outside the range any device dump or our
  own upload ever touches, and even Valeton's own tool doesn't reproduce it
  export-to-export, so there's no "correct" value to aim for here.

## 2. Messaging and commands (SysEx protocol)

- Header: `F0 21 25 7E 47 50 2D 32`.
- Handshake required once per connection before writes are honored:
  identity query (cmd=0x11/sub=0x04), then "enter editor mode"
  (cmd=0x11/sub=0x12). Reads work without this; writes are silently
  discarded without it.
- 7-bit-per-byte offset encoding for multi-chunk transfers:
  `(byte1 & 0x7F) | ((byte2 & 0x7F) << 7)`.
- Nibble encoding: each byte → (high nibble, low nibble) as separate
  SysEx-safe (≤0x7F) bytes.
- No ACK/NAK on the write path at all — confirmed by inspecting both
  GP200 Studio's and RigSheet's own write loops; both just blast chunks at
  a fixed interval and verify only via a full separate re-read afterward.
  Reads ARE self-confirming (explicit numbered chunk responses).
- Write addressing (the RigSheet-vs-GP200-Studio resolution, the session's
  central protocol fix): outer per-chunk header byte (position 10, right
  after cmd=0x12/sub=0x20) is a FIXED CONSTANT 0x09 on both reads and
  writes — NOT the target slot, as GP200 Studio's own code assumed. The
  real slot lives inside the nibble-decoded payload's own 14-byte inner
  header, at relative offsets 6 and 12. GP200 Studio blanks these to 0xFF
  instead, which appears to be why its model's writes were silently
  discarded by the device.

## 3. Reliability (open, actively evolving — "necessary but not sufficient")

Getting sections 1 and 2 right was *necessary* to get writes to stick at
all (before the RigSheet-model fix, writes were always silently
discarded), but real-hardware testing shows a protocol-correct write still
occasionally doesn't verify — i.e. protocol correctness alone is not
*sufficient* for a reliable write. This section exists because that gap is
real and worth documenting honestly, not glossing over. Layers
investigated so far, in the order considered:

1. **Settle time after the chunk burst.** Tested via a dedicated
   `calibrate-settle` stress test (sweeps the post-write delay, tallies
   which offset(s) mismatch). Real result: failures recurred at 1.00s,
   1.10s, 1.20s and 1.30s settle delays — NOT eliminated by waiting longer.
   Rules out "just needs more time" as the sole explanation.

2. **Which byte fails.** In that same run, EVERY mismatch, at every delay,
   landed on the exact same file offset: 0x9F (see section 1 — always
   0x00 in every real sample). A specific, reproducible weak point, not
   scattered random corruption. Values seen there (0x36, 0xB6, 0xB7) share
   bit patterns suggestive of a reconstruction issue rather than a generic
   wire-level bit flip (nibble-encoded bytes on the wire are always ≤0x0F,
   so the corrupted byte's high bit couldn't have been set by anything
   touching the raw SysEx stream directly).

3. **Host-side MIDI stack as a candidate.** python-rtmidi's Windows backend
   uses a small number of FIXED-SIZE preallocated buffers for incoming
   SysEx via the Windows Multimedia API — a documented, real category of
   flakiness independent of any specific device:
   - https://github.com/SpotlightKid/python-rtmidi/issues/200
   - https://github.com/mido/mido/issues/207
   Our messages (max 384 bytes) are well under the documented size
   thresholds in those issues, so this exact failure mode may not be it,
   but buffer-reuse timing on the RECEIVE side remains a plausible
   mechanism for a stale byte at a consistent relative position. A
   `reread` command (reads the same slot repeatedly with NO writes at
   all) was built to test this directly: if plain re-reads of an
   unchanged slot ever disagree with each other, that's host-receive-side,
   not device-write-side, since nothing was written in between.

   Broader research (2026-09-27) confirms this general area is a well-
   documented minefield, not something specific to us or the GP-200:
   - https://github.com/thestk/rtmidi/issues/387 (rtmidi maintainers' own
     tracking issue) confirms SysEx handling is inconsistent across every
     backend (ALSA, WinMM, CoreMIDI, JACK, Windows MIDI Services): different
     size limits, inconsistent message/fragment reassembly, and -- a new
     candidate mechanism -- **real-time status bytes (e.g. MIDI Active
     Sensing, which many devices periodically emit just to signal they're
     alive) can interleave into an in-progress SysEx stream and corrupt
     it**, independent of buffer size or power entirely. Also confirms
     Windows' classic WinMM API (what python-rtmidi uses by default) has
     extra complexity around "long messages" (buffer prepare/unprepare,
     MIDIERR_STILLPLAYING while a transfer is still in flight).
   - https://github.com/mido/mido/issues/240 -- a different device (Nord
     Drum) flatly rejected multiple SysEx messages sent back-to-back
     through mido's loop; the reporter had to work around it with an
     external tool. Different device, same shape of problem, and consistent
     with our own decision to pace chunks 40ms apart rather than blast them.
   - https://github.com/microsoft/MIDI/issues/1041 -- a confirmed, specific
     2026 bug where Windows' *newer* MIDI 2.0 driver stack corrupts
     back-to-back SysEx by misreading a fresh SysEx start byte as
     continuation data. Almost certainly not what we're hitting (it's a
     newer, optional driver path, not the classic WinMM one python-rtmidi
     defaults to), but shows Windows' MIDI stack has shipped real SysEx
     corruption bugs before, not just theoretical risk.
   If the power-supply test (below) comes back negative -- no difference
   between supplies -- the Active Sensing interleaving idea above is the
   next thing worth testing, since it's a plausible mechanism that has
   nothing to do with power at all.

4. **Physical/power layer — TESTED, RULED OUT (2026-09-27).** User had
   intermittently seen changes save correctly in the OFFICIAL Valeton
   Desktop software's own UI but NOT actually land on the device
   (previously assumed user error). Power setup varies: stock supply (1A,
   reported mediocre) vs a USB power bank (22.5W) with a 5V→9V
   "upconverter" cable. Hypothesis was that a flash write's current spike
   could cause a supply-rail sag on the cheaper/less-regulated path,
   corrupting the write in progress.

   **Test**: `soak` (fixed settle delay, N repeated write+verify cycles,
   failure RATE only, no variable changes based on outcome), 3 runs of 20
   on the stock supply, 3 runs of 20 on the battery rig, same computer/
   cable/slot/file throughout.

   **Result**: stock supply 9/60 failed (15%); battery 9/60 failed (15%).
   Identical aggregate rate. Per-run spread (stock: 15/20/10%; battery:
   30/0/15%) is well within ordinary binomial sampling noise for n=20 at a
   true ~15% rate (expected SD ≈8 points) — not evidence of a real
   difference. Even more telling: pooling all 18 mismatches across BOTH
   power sources, every single one was still in the same narrow value
   range as before (0x36/0x37/0x38, sometimes with the high bit set as
   0xB6/0xB7/0xB8) — the corrupted value's character didn't change at all
   between power sources. A voltage-sag/bit-flip mechanism would be
   expected to change what garbage shows up when the supply changes; this
   didn't. **Conclusion: power supply is not a meaningful factor.** Safe
   to use either supply for real bulk work; this line of investigation is
   closed unless future evidence reopens it.

   The value clustering itself is now the most interesting open clue: it
   looks less like random bit-level corruption and more like the GP-200
   occasionally leaving some *other* piece of its own internal state (a
   counter, a cursor, something slowly drifting across a session) sitting
   in this byte instead of zeroing it before the write commits.

5. **`reread` test — RUN, POSITIVE (2026-09-27): host-side receive glitch
   confirmed, not device/flash storage.** `reread 37-A --count 10`, zero
   writes at all: read #3 disagreed with all other 9 reads, same 0x9F
   byte, same corrupted-value character (0xB7) as every prior finding.
   Since nothing was written between any of the 10 reads, the device's own
   flash content cannot have changed between read #2 and read #3 — the
   disagreement can only be explained by something on the host's receive
   side (python-rtmidi / the OS MIDI stack), not by the GP-200's write path
   or its stored data. This is the single most important result of the
   whole investigation: it means at least *some* — quite possibly most —
   of the "write reliability" problem this session has been chasing was
   actually a **read-verification artifact**, not a write/flash defect.
   Every prior finding is consistent with this: byte 0x9F is a fixed
   location for it, the corrupted value is a narrow, repeatable range
   (0x36–0x38, ±0x80) rather than random noise, and it happens identically
   regardless of power supply — all things a specific host-side buffer/
   timing quirk would produce, and none of which rule out "and it might
   *also* sometimes be a real write failure" (see the note on this below).

6. **Fix: `Device.read_dump_confirmed()` — re-read until any two agree, or
   say so explicitly (2026-09-27, revised same day).** Direct response to
   finding 5: a lone read can no longer be trusted as ground truth for
   anything that cares about byte-for-byte accuracy.

   First version: read, then keep re-reading until two reads *in a row*
   matched, up to a small extra-reads budget, returning the last attempt if
   the budget ran out. Revised the same day after realizing that had two
   real problems: (1) checking only *consecutive* pairs misses agreement
   that isn't adjacent — e.g. reads `[A, B, A]` have the 1st and 3rd
   agreeing, which is real signal, but a consecutive-only check would
   never notice it; (2) when reads never agreed at all, it silently
   returned its last attempt anyway, giving callers no way to tell an
   arbitrary unconfirmed guess apart from an actually-confirmed read.

   Current version, `read_dump_confirmed(slot, tries=3)`: takes up to
   `tries` reads (minimum 2, default 3 — a 3-reads/2-must-agree scheme),
   returns as soon as ANY two of them (not just adjacent ones) come back
   byte-identical, and raises `ReadNotConfirmedError` (a `TimeoutError`
   subclass, so existing `except TimeoutError` handling keeps working
   unchanged) if none of the `tries` reads ever agree with each other —
   rather than ever handing back an unconfirmed guess. Wired into the same
   three places as before:
   - `Device.verify_write_full` — so `write_and_verify`'s retry loop (used
     by `upload`/`import`/`apply-template`, `calibrate-settle`, `soak`) is
     no longer retrying writes that were already correct, just because the
     one confirming read happened to glitch. On the new "never agreed"
     outcome, it's now reported distinctly ("reads never agreed with each
     other") rather than folded into the generic "no response" message,
     which used to describe only an actual device timeout.
   - `cmd_export` (both the single-slot and `--all` bulk paths) — so a
     backup no longer has a ~15%-per-read chance of silently saving a
     byte-wrong copy, which is the one failure mode a backup tool must not
     have. The single-slot path previously had NO exception handling
     around this call at all (a real failure would have crashed with a raw
     traceback); it now exits cleanly with a clear message, same as every
     other hard failure in this tool.
   - `cmd_diag_write`'s pre-write backup-read, for the same "accurate
     backup" reason, and likewise previously unguarded. Now, if the backup
     can't be confirmed, the command aborts *before* writing anything —
     proceeding with a destructive write with no reliable safety copy
     behind it would be worse than just stopping.
   Deliberately NOT applied to `cmd_reread` itself — that command's entire
   purpose is to expose raw read-to-read disagreement, and confirming its
   reads would silently launder away the exact instability it exists to
   surface. (It's the tool that *found* this issue; it should keep finding
   it.)

   Read and write failures are not mutually exclusive, and confirming two
   reads agree is not proof a write actually failed when they disagree
   with the source — see finding 7 below for the follow-up test built to
   tell those apart, prompted directly by that concern.

7. **Open: is any of this ALSO a real write-side failure, some of the
   time?** Finding 5 proves read glitches are real; it does not prove
   writes are always fine. Two consecutive agreeing reads makes a pure
   read-glitch explanation for any given mismatch much less likely (roughly
   (measured single-read failure rate)² if the two reads' errors are
   independent), but "less likely" isn't "impossible," especially across
   many slots/many runs — and it's exactly the kind of gap that matters
   when comparing hand-exported .prst files rather than running the same
   automated check 20 times in a row. `soak` was extended with
   `--confirm-reads N` and `--stop-on-failure` for this: on any mismatch
   (i.e. one that already survived `verify_write_full`'s own two-read
   confirmation), it does N MORE no-write re-reads and reports whether they
   agree with the bad value (STORED — points at a real write-side problem),
   come back matching the source (the mismatch was itself transient noise
   that happened to repeat twice), or scatter (still can't be pinned down).
   `--stop-on-failure` halts the run right after that check so a real
   hardware discrepancy can be inspected, or the device power-cycled and
   re-read, before anything else overwrites it. Built and unit-tested
   2026-09-27.

   **First real-hardware run (2026-09-27):** `soak 37-A test_patch.prst
   --count 20 --confirm-reads 5 --stop-on-failure`, same slot/settle/file
   pattern as the original power A/B test. Result: **1/20 failed (5%)** —
   notably lower than the ~15% baseline measured before `read_dump_confirmed`
   existed, consistent with finding 5/6's expectation that read noise was
   inflating the old rate. The one mismatch (again byte 0x9F, value 0x35)
   resolved as **transient**: all 5 confirmation re-reads matched the source
   file exactly, none matched the bad value. First real evidence in this
   investigation's favor for "the write path may be more reliable than it
   looked" — but one clean result isn't a rate; the plan (per finding 4's own
   methodology) is still to run this several more times before treating 5%
   as real, and to watch specifically for a run where confirmation reads
   report STORED instead, which would flip this conclusion.

   One nuance worth flagging: `verify_write_full` already requires two
   consecutive reads to *agree* before reporting a mismatch at all — so in
   this run, the device returned 0x35 on two back-to-back reads before the 5
   independent follow-up reads all came back clean. That's a slightly
   stronger coincidence than a single stray glitch, and reads less like
   "each read independently rolls the dice on a random bad byte" and more
   like "a stale receive buffer gets handed back for a couple of rapid
   successive read attempts, then clears" — i.e. still consistent with a
   host-side buffer-reuse artifact (finding 3), just with a short persistence
   window rather than being purely per-call independent. Doesn't change the
   verdict (still not stored on the device), but matters for how confident a
   future two-read agreement should be trusted as "real."

   **Second real-hardware run (2026-09-27), same command/slot/file:**
   mismatch on cycle 2/20 this time (byte 0x9F again, value 0xB8 -- same
   corrupted-value cluster as every prior finding), again resolved as
   transient: all 5 confirmation reads matched the source, none matched
   0xB8.

   **Third real-hardware run (2026-09-27), same command/slot/file:**
   mismatch on cycle 4/20 (byte 0x9F, value 0xB8 again), again fully
   transient.

   **3-run tally, matching the three-runs-per-condition standard used for
   the power A/B test: 3/60 total mismatches (5% every run, no variance at
   all), 0 STORED verdicts.** Every single mismatch across all three runs
   resolved as read-side noise -- not one has ever come back confirmed as
   stored on the device. Combined with the halved failure rate versus the
   pre-`read_dump_confirmed` baseline (~15% -> ~5%), this is now reasonably
   strong evidence -- not proof, but a real, repeated, consistent result --
   that the GP-200's actual write/flash path has been reliable throughout
   this investigation, and essentially all of the "reliability problem"
   this session set out to chase was a host-side read-verification artifact
   (see finding 5/6). This doesn't retroactively explain the pre-fix Valeton
   Desktop software incidents the user described (those predate this
   fix and were never directly tested), but it does mean the default
   assumption for THIS tool's own writes should now be "trust the write
   path; the residual ~5% is a read/verification quirk, not corruption,"
   rather than the reverse.

   **Supplementary run, same command, AFTER the finding 8 fix (2026-09-27):**
   mismatch on cycle 3 (byte 0x9F, value 0x37), `--stop-on-failure` halted
   the run there as designed; all 5 confirmation re-reads matched the
   source, so again transient, 0 STORED. Cut short at 3 of the planned 20
   cycles, so it's a data point alongside the 3-run tally above, not a
   fourth full run added to it -- but notable for being the first
   `--confirm-reads` run recorded fully on the `raw_dumps_agree`-fixed code
   end to end, and it lands exactly on the same 0x9F/transient pattern as
   every run before it. (This run's own reported "1/20 failed (5%)" summary
   line was wrong -- see finding 10 -- the real rate for what actually ran
   was 1/3.)

8. **Bug found and fixed the same day: `read_dump_confirmed` compared raw
   dumps with NO filtering, and the tail block turns out to change on every
   plain READ, not just every save (2026-09-27).** Right after finding 7
   closed out, re-running `calibrate-settle` (the very first stress test
   this session, being run for the first time ever with `read_dump_confirmed`
   wired in) failed to verify on EVERY attempt -- 13/13 -- reporting "reads
   never agreed with each other." Follow-up no-write `export` calls on the
   same idle slot failed 3/3 the same way, while `reread` on that same slot
   (which doesn't use `read_dump_confirmed`) looked completely normal. That
   ruled out both a global hardware/session problem and a general read-side
   regression, and pointed squarely at `read_dump_confirmed` itself.

   Adding full (non-truncated) raw SysEx logging to `-d` and diffing three
   real reads by hand found the answer: the bytes that differed were at file
   offsets `1120+q*12+6` and `+7` for `q in 0..7` -- exactly the already-
   documented **tail block** (see section 1 and `VERIFY_IGNORE_OFFSETS`),
   previously characterized only as changing on every *save*. It turns out
   to also change on every plain *read*, a new piece of information this
   forced out into the open. `read_dump_confirmed` (added earlier the same
   day) was the first piece of code in this project to ever compare two raw
   device dumps directly against each other -- every earlier comparison went
   through `diff_prst_content`, which already excludes this region. Nobody
   had taught the new function about it, so it was comparing a field that
   can never be expected to repeat, making confirmation fail (in the worst
   observed case) 100% of the time on a completely untouched slot.

   Fix: `RAW_DUMP_IGNORE_OFFSETS` (a translation of `VERIFY_IGNORE_OFFSETS`
   into raw-dump-relative coordinates) and `raw_dumps_agree(a, b)`, used by
   `read_dump_confirmed` and by `_describe_raw_dump_diff`'s diagnostic
   output, in place of a bare `==`. Regression-tested: dumps differing only
   in the tail block now correctly confirm as agreeing; a real difference
   elsewhere (e.g. byte 0x9F) is still correctly caught even alongside
   tail-block noise.

   **Re-verified on real hardware the same day, both tests that originally
   exposed the bug:**
   - `export` (single no-write read of an idle slot): clean, confirmed in
     just 2 reads, produced a normal-looking `.prst` file. No "reads never
     agreed" failure.
   - `calibrate-settle 37-A --force` -- the exact command whose 13/13
     failure kicked off this whole finding -- completed its full 20/20
     target streak on the very first attempt at the starting 1.00s settle
     delay, zero reported mismatches. Four times during the run,
     `read_dump_confirmed` needed a 3rd read to reach agreement, every time
     due to the same well-understood transient 0x9F glitch from findings
     2/5 (values seen: 0xB7, 0x36, 0xB6, 0x37) -- and every time fully
     absorbed by the 3rd read matching one of the first two, never surfacing
     as a mismatch to the calibrate-settle loop itself. This is the expected
     signature of read noise being filtered correctly, not luck -- the
     script's own generic closing remark about "may have gotten lucky" was
     written without knowledge of this fix and can be disregarded for this
     run.
   This closes the loop opened at the top of this finding: the fix works
   end-to-end on real hardware, on the exact test that first exposed the
   regression.

   Worth noting: this bug is specific to `read_dump_confirmed`'s raw
   comparison and does NOT retroactively cast doubt on finding 7's 3-run
   result -- `soak`'s reported mismatches there went through the filtered
   `diff_prst_content` comparison same as always, and `reread`'s own
   findings have always used that same filtered path. The one open question
   this raises: the OLD (pre-today) `read_dump_confirmed` had this identical
   filtering gap and mostly still worked in finding 7's three runs (~95%
   immediate success) rather than failing constantly like this -- suggesting
   the tail block's update timing may be closer to "ticks roughly every so
   often" than "changes on literally every single read call," making
   agreement-by-luck common within one write-cycle's tiny read window but
   apparently not within this session's later, more sustained testing. Not
   fully understood yet; the raw_dumps_agree fix sidesteps needing to know
   the exact mechanism, same as read_dump_confirmed originally sidestepped
   needing to know why the host receive side glitches at all.

9. **`export` now zeroes the tail block, matching Valeton's own export
   behavior (2026-09-27).** Prompted directly by a user question: since the
   tail block changes on every read (finding 8), two exports of the exact
   same untouched patch, taken seconds apart, would previously come out
   byte-different every time -- and, more importantly, a comparison against
   Valeton's own official export of that same patch would also show a
   difference here, purely from this device-owned noise. To someone who
   doesn't already know this investigation's history, that reads exactly
   like a bug in this tool, not the (correct) explanation that the field is
   meaningless scratch state.

   This was close to the user's own hunch from finding 8 ("Valeton zeros out
   some parts of the prst when exporting that do not actually control patch
   parameters") -- that hunch was about the mechanism (device-owned,
   non-patch-controlling fields), and turned out to describe the tail block
   almost exactly, even though the specific bug that day turned out to be
   about *comparison* filtering, not export content.

   Fix: `TAIL_BLOCK_FILE_OFFSETS` (the tail-block magic numbers, pulled out
   of `VERIFY_IGNORE_OFFSETS` into their own named constant so both
   consumers share one definition) and `normalize_export_dynamic_fields()`,
   which zeroes those bytes and recomputes the checksum. Wired into `export`
   only (both the single-slot and `--all` paths) -- deliberately NOT applied
   to `verify_write_full`'s internal roundtrip comparison or its saved
   failure diagnostics, which still want the device's real, unmodified
   reported value; a debugging aid should show what's actually there, not a
   normalized version of it. `read_dump_confirmed`'s own comparison logic
   (`raw_dumps_agree`) is also untouched -- this only changes what `export`
   writes to disk, not how any read is confirmed or any write is verified.

   Since section 1 already established the device recomputes this block on
   every save regardless of what was written, a zeroed export loses nothing
   that a later re-upload of that file could have preserved anyway -- the
   device was always going to overwrite it.

   Unit-tested: two dumps differing ONLY in tail-block noise now normalize
   to byte-identical exported files; real content elsewhere in the file is
   provably untouched; the checksum is correctly recomputed (and provably
   changes vs. the un-zeroed version, ruling out a stale-checksum bug);
   `diff_prst_content` (which already ignores this region) sees no
   difference between the zeroed and un-zeroed versions, confirming the
   change is invisible to write verification, as intended.

   **Real-hardware validation and extension (2026-09-27), from an actual
   Valeton Desktop export of the same slot/patch (37-A "Blue Sparkle") the
   user provided for direct comparison:** the tail-block zeroing matched
   exactly -- all 16 bytes read `0x00` in both our export and Valeton's.
   That comparison also surfaced a second, narrower and previously-
   undocumented difference: file offset 0x2E read `0x00` in Valeton's
   export but the real slot value (`0x90` for slot 37-A) in ours, while the
   OTHER two bytes long grouped with it as "the slot-mirror bytes" (0x34,
   0x90) matched at the real slot value in BOTH files. A SECOND independent
   Valeton export of the same untouched patch, run specifically to check
   whether this was coincidence, agreed exactly: 0x2E was `0x00` again, 0x34
   and 0x90 were the real slot value again, and the tail block was zero
   again. (The only bytes that differed between the two Valeton exports
   were 0x1C-0x1F, confirming that region is genuinely dynamic even for
   Valeton's own tool -- not something either exporter could match, and not
   attempted.)

   Two independent, agreeing real exports is strong evidence for a
   deterministic byte value -- a different and much cheaper kind of
   confirmation than the noisy read/write reliability rates elsewhere in
   this document, which needed many trials specifically because they're
   statistical, not deterministic. Worth flagging plainly, though: this
   finding comes from comparing EXPORTED FILES, not from testing what our
   own write path does with 0x2E on real hardware -- it says Valeton's
   exporter treats 0x2E as zero-worthy on the way OUT, not that the device
   requires or even accepts a particular value at 0x2E on the way IN. That
   sits oddly next to `build_upload_image`'s own docstring, which -- based
   on the earlier RigSheet cross-check -- calls 0x2E "ordinary content...
   not one of the fields the device recomputes itself." That characterization
   is now flagged as an open question in that docstring rather than silently
   left standing, but NOT changed, since changing write behavior on the
   strength of an export-side comparison, without a dedicated write-side
   test, would be exactly the kind of unconfirmed leap this document has
   been trying to avoid all session.

   Fix (export-side only): `EXPORT_ZEROED_SLOT_ECHO_OFFSET` (0x2E) added to
   `normalize_export_dynamic_fields`'s zeroing, alongside the tail block;
   0x34 and 0x90 deliberately left untouched, matching what both real
   Valeton exports actually showed. Unit-tested (0x2E zeroed, 0x34/0x90
   provably not, two dumps differing only in tail-block+0x2E noise still
   normalize identically), and re-verified directly against the two real
   Valeton export files: after this fix, the only remaining differences
   between our export and Valeton's are the already-understood, unavoidable
   ones -- the dynamic 0x1C-0x1F header region and the Valeton-only 0x28-0x2D
   stamp (plus the checksum, which trails both).

   **Confirmed on a SECOND, different patch and slot (2026-09-27):** a
   Valeton export and our own tool's export of 18-D "Demo Shimmer" (a
   different patch, different slot, different effect-block content from
   37-A "Blue Sparkle") were compared the same way. Result: 11 differing
   bytes total, every single one inside the already-known 0x1C-0x1F/0x28-0x2D/
   checksum territory -- zero unexpected differences. Tail block zero in
   both, 0x2E zero in both, 0x34/0x90 both `0x47` (the real slot value for
   18-D) in both, patch name and all effect-block content identical. This
   is the fix generalizing across two independent patches/slots, not just
   the one (37-A) that originally surfaced it -- as solid a confirmation as
   file-comparison testing can practically give without instrumenting
   Valeton's own exporter directly. The open item asking for exactly this
   second-patch check is now closed.

10. **Bug found the same day, while investigating the above: `soak
    --stop-on-failure`'s summary line used the configured `--count` as the
    denominator instead of the cycles actually attempted.** Caught directly
    from a real hardware run's pasted output: `--stop-on-failure` halted the
    run at cycle 3/20 (a mismatch on cycle 3, confirmed transient via the
    5 follow-up reads), but the closing line read "Result: 1/20 failed
    (5%)" -- as if all 20 cycles had run. The true rate for that run was
    1/3 (33%), a very different number from 5%, and a misleading one for
    anyone using `soak`'s reported rate to compare conditions (its whole
    purpose, per finding 4's own methodology).

    Fix: track `cycles_run` (the loop counter's value at whatever point it
    stopped, whether that's a full `--count` or an early `--stop-on-failure`
    break) and report the rate over that instead of `args.count`; the
    summary now also appends a `(N attempted before stopping)` note whenever
    the run ended early, so it's never ambiguous which number a bare
    "M/N failed" refers to. Unit-tested; a full, non-early-stopped run's
    summary is unaffected and still reports `M/--count`, with no
    "attempted before stopping" note.

11. **Fix: `read_dump_confirmed`'s default `tries` raised 3 -> 5 (2026-09-27),
    after a real 256-slot `export --all` run exposed it as a real, if
    modest, reliability gap.** 9 of 256 slots (~3.5%) hit "3 reads never
    agreed with each other" and were skipped -- not data loss (read-side
    only), but a real usability problem for a tool whose whole point is a
    trustworthy bulk backup: "just re-run the export" isn't a great answer
    at 256-slot scale, and the user reasonably asked whether a 3-tries/
    2-must-agree scheme is actually as safe as it sounds.

    The math says the 3.5% is exactly what a 3-tries scheme predicts, not a
    bug: confirmation needs only 2 *clean* reads to agree (every clean read
    sees the same true content), so "never agree" happens whenever at most
    1 of the `tries` reads is clean. With finding 6's independently
    measured ~15%-per-read glitch rate (call it p), P(never agree) ≈
    p^tries + tries·(1-p)·p^(tries-1) -- the chance of 0 or exactly 1 clean
    read. Solving that formula backward from the observed 3.5% at
    `tries=3` gives an effective p ≈ 0.11-0.14, consistent with finding 6's
    figure. Plugging the same p into `tries=5` predicts roughly 0.07-0.1% --
    a ~35-50x drop -- because going from "need >=2 bad out of 3" to "need
    >=4 bad out of 5" is a much rarer event at this glitch rate. Crucially,
    this costs nothing in the common case: `read_dump_confirmed` still
    returns as soon as ANY two reads agree, so the large majority of slots
    that already confirm within 2 reads see zero extra cost; only the
    minority of genuinely glitchy slots pay for the extra 1-2 reads.

    Considered and rejected: reconstructing a "majority vote per byte"
    across all `tries` reads instead of requiring two whole-dump reads to
    match exactly. More complex, harder to reason about and test, and not
    obviously needed -- the "any two full reads agree" design already
    handles the dominant failure mode (an isolated single-byte glitch on an
    otherwise-clean read) correctly, since 2 clean reads will always exactly
    agree with each other regardless of how many corrupted reads also
    happened. Not implemented.

    One residual risk this fix does NOT eliminate, flagged for honesty
    rather than because it's been observed causing a problem: the scheme
    assumes two independently glitched reads essentially never coincide on
    the exact same wrong byte value. The known trouble spot (pre-effects
    header byte 19, file-relative 0x8C-0x9F, "always 0x00 in every real
    sample seen so far") shows glitch values clustering around a fairly
    small repeating set (0xB6-0xB8, 0x35-0x38 have all been observed) rather
    than being fully random across 0-255, so a coincidental false-agreement
    on a wrong value isn't mathematically impossible. It should still be far
    rarer than the now-fixed problem, since any one specific wrong value is
    much less likely per read than the correct value is -- but this hasn't
    been separately measured, only reasoned about.

    Not yet re-verified at the same 256-slot scale against real hardware
    with the new default -- next `export --all` run should show the skip
    count drop sharply (ideally to 0) if the model above is right.

    **Supporting evidence (2026-09-27, still on the old tries=3 default):** a
    second, independent `export --all` run (still pre-fix) skipped 3/256
    slots (18B, 26B, 33D) -- a different count (3 vs. 9) and, notably, ZERO
    overlap with the first run's 9 skipped slots (12D, 19B, 23A, 25B, 27A,
    31B, 38D, 54D, 55B). Zero overlap across two independent runs is exactly
    what pure per-read host-receive noise predicts (each slot's failure is
    an independent roll, unrelated to what's actually stored there) and
    weighs against any theory involving a specific slot, address, or stored
    value being the culprit -- consistent with, though not new proof beyond,
    finding 5's original host-side diagnosis.

**Working conclusion so far**: protocol correctness (sections 1–2) was
necessary but does not appear to be sufficient on its own for *perfect*
reliability — though the picture is now considerably more encouraging than
where this investigation started. Power delivery was tested and ruled out
as the cause. The `reread` test (finding 5) proved the host's MIDI receive
side is genuinely unreliable at a measurable, repeatable rate. `soak
--confirm-reads` (finding 7), run three times against real hardware, found
zero cases of a mismatch actually being stored on the device — every one
resolved as read-side noise, at a rate (~5%) roughly half the pre-fix
baseline. A same-day regression in `read_dump_confirmed` itself (finding 8)
briefly made the fix look broken (100% confirmation failure), but was
root-caused (an unfiltered comparison against the already-known "tail
block," which turns out to change on every read, not just every save) and
fixed the same day, then re-verified clean on real hardware on both tests
that had exposed it: an idle `export` confirming in 2 reads, and
`calibrate-settle` — the very first stress test of this whole
investigation — completing its full 20/20 streak at the first settle delay
with zero mismatches. Taken together, this is now a reasonably
well-supported conclusion, not just a plausible theory: the GP-200's actual
write/flash path appears to be reliable, and what looked all session like
"the device sometimes doesn't save" was, as far as this testing can show,
actually "the read used to check the save sometimes lied." `read_dump_confirmed`
(finding 6, fixed in finding 8) filters that noise out before it can be
misreported as a write failure or silently corrupt a backup, and normal
`upload`/`import`/`apply-template` usage keeps its verify+retry safety net
regardless (MAX_WRITE_ATTEMPTS=10, cheap insurance either way, and worth
keeping even though writes now look solid). This doesn't retroactively
explain the pre-fix Valeton Desktop software incidents the user originally
raised — those happened with different software, before this fix existed,
and were never directly tested — so "the write path is probably fine" is
this tool's own finding about its own write path, not a blanket claim about
the device or about every piece of software that talks to it. When the
manual is eventually written, section 3 should state plainly that the
dominant reliability risk found here was in reading the device back, not in
writing to it — a materially different, and better, story than where this
investigation started.

12. **GP200 Studio's own "Export All" silently drops any patch containing a
    NaN effect parameter -- unrelated to this project's read-reliability
    work (2026-09-27).** Prompted by comparing a full 256-slot `export --all`
    against GP200 Studio's own "Export All" on the same device: Studio
    produced 245 entries, missing 11 real, meaningfully-named patches (15A
    "Fat Bird (Wah)", 15C "Where Am I", 15D "80s Disco", 23D "Spicy Bass",
    24B "Mouse Bass", 24C "Slow Bass", 24D "Angry Bass", 25A "Summer Bass",
    25B "Bit Bass", 25C "Rock Bass", 30A "Tata Early1"). Run twice
    independently; the missing-slot list was byte-for-byte identical both
    times -- ruling out the transient host-receive read noise this project
    has been chasing (finding 5 onward), since that noise produces a
    *different* random set on each run (confirmed by two of this tool's own
    pre-fix `export --all` runs having zero overlap in their skipped slots).
    A deterministic, repeatable omission means something about those
    specific patches, not read luck.

    Root cause, confirmed by direct inspection: every effect block stores 15
    per-parameter IEEE-754 float32 values at file offset
    `0xA0 + block*0x48 + 0x0C + param*4` (this project's own `decode_prst`,
    ported from GP200 Studio's own PRSTDecoder format). Scanning all 256
    slots' raw parameter floats for non-finite (NaN/Infinity) values found
    exactly 11 slots with any non-finite parameter -- a perfect 1:1 match
    with Studio's 11 skipped slots, no exceptions in either direction. This
    tool's own device reads return these NaN bytes with no error at any
    layer (correct checksum, correct length, clean `read_dump_confirmed`
    agreement) and exports them untouched, since export never numerically
    interprets parameter floats -- it copies device dump bytes onto the
    skeleton and only touches the header/tail-block regions covered by
    `normalize_export_dynamic_fields`. The device itself is evidently fine
    storing and returning NaN; whatever GP200 Studio's export path does with
    a parsed patch (presumably serializing/validating the numeric value)
    apparently isn't, and it fails silently rather than raising a visible
    error, dropping the patch from the export with no indication to the
    user.

    NaN positions are consistent per effect ID across different patches
    using the same effect (e.g. effect `0x00000001` always shows NaN at
    exactly parameter indices 4, 6, 10, 12 across 6 different patches) --
    strong circumstantial evidence NaN is the device's own sentinel for "this
    parameter slot doesn't apply to this effect type" (effects apparently
    don't all use all 15 available parameter slots), not corruption. This
    also retroactively explains why this project's own `decode_prst` already
    carried a defensive `if math.isfinite(raw) else 0.0` guard on every
    parameter read (written earlier in this project for the write/live-edit
    path, before this specific finding) -- that guard turns out to matter for
    real, currently-existing factory/user patches, not just a theoretical
    edge case.

    This is a genuinely different failure mode from everything else in this
    document: not a MIDI transport or read-timing issue at all, but an
    apparent bug in GP200 Studio's own export-side patch parsing, one this
    project's independently-developed, byte-copying export approach happens
    not to share. Worth reporting upstream (see the drafted issue delivered
    to the user); not something to "fix" in this tool, since this tool
    already handles it correctly by construction.

13. **Fix implemented: `STUDIO_ADDITIONAL_ZEROED_OFFSETS` (2026-09-27).**
    Direct follow-through on the "extend the zero set" recommendation from
    the 245-patch GP200 Studio comparison above. Added a new constant
    (0x3E, 0x40, plus `+5`/`+10`/`+11` within each of the 8 already-tracked
    tail-block quads -- 26 positions total) and wired it into
    `normalize_export_dynamic_fields` alongside the existing tail-block and
    0x2E zeroing. Kept as its own constant rather than merged into
    `TAIL_BLOCK_FILE_OFFSETS`, for the same reason `EXPORT_ZEROED_SLOT_ECHO_OFFSET`
    is separate: different evidence (file comparison, not direct read/write
    testing), so it's applied only to what `export` writes, not to
    `VERIFY_IGNORE_OFFSETS` -- the write path's behavior at these offsets is
    unproven and deliberately left alone, exactly as with 0x2E.

    Deliberately did NOT include offset `+9` per quad (partial signal only --
    Studio zeroes it ~92% of the time, not the clean 100% every other
    position here shows -- more likely occasional real content or read
    noise than an export convention) or the small residual anomalies (file
    offsets 146/147, and the couple of one-off differences just before the
    documented 8-quad series starts). Those remain open, flagged rather than
    silently absorbed into the fix.

    Re-verified against the same real 245-patch GP200 Studio comparison:
    total mismatched bytes across all 245 common patches dropped from 732 to
    342 (53%), and slots with any remaining difference dropped from 128/245
    to 121/245 (some slots have both a fixed offset and a still-open one,
    like `+9`, so the per-slot count drops less than the byte count). Every
    remaining mismatch is one of the explicitly-deferred loose ends above --
    nothing new appeared. Unit-tested (26-position count, no overlap with
    the existing tail-block/0x2E offsets, zeroing applied correctly, and an
    explicit check that these new positions are NOT hidden from
    `diff_prst_content`, unlike the original tail-block/0x2E zeroing --
    proving write verification stays untouched by this export-only change).

14. **GP200 Studio's own pull path has no read-agreement check at all
    (2026-09-27).** Following up on finding 12, the user asked whether this
    project's own read-confirmation logic (finding 6/8, `tries=5` per
    finding 11's math above) could be retired now that Studio appears to get
    consistent results without it. Reviewed Studio's own published source
    (github.com/kabir0st/gp200-studio, GPL-3.0, a third-party open-source
    browser-based GP-200 editor -- not Valeton's own official app, and not
    the "RigSheet" reference project this investigation has been careful to
    keep separate from; see the note below) to see what its read path
    actually does, independent of anything this project already believed
    about it.

    Finding, from reading the pull-a-patch function and its supporting
    decode path: there is no multi-read comparison anywhere in it. A single
    read is requested; if no reply arrives within its timeout, it retries
    the request exactly once (a timeout retry, not a content-mismatch
    retry); whatever bytes come back are decoded and used with no checksum
    check and no comparison against any other read of the same slot. A
    separate function elsewhere (used only after *writing* a patch) does a
    single readback and compares just the patch name and CTRL footswitch
    assignments against what was pushed, to catch an outright-discarded
    write -- narrower in scope than, and not a substitute for, this
    project's own full-content, multi-read `read_dump_confirmed`.

    This resolves the user's question directly: Studio's export runs are not
    evidence that read glitches aren't real, only that Studio doesn't check
    for them. It has no mechanism that could distinguish a clean read from a
    glitched one, so a glitched read there is simply used as-is; whether
    that shows up depends on where in the file the glitch happens to land
    (this project's own `reread` test, finding 5, found glitches
    concentrated in specific header bytes that Studio's export may not
    surface visibly, e.g. because they fall outside what it displays or
    re-serializes). This project's own retry protocol is not made redundant
    by anything found here -- if anything, it's the only one of the two
    tools that actually checks.

    Note on the two GitHub projects: Studio's `src/components/RigSheet.tsx`
    is unrelated to the "RigSheet" project this investigation has treated as
    a licensing boundary throughout ("all rights reserved," never to be
    copied from). It's a printable one-page cheat sheet of the current
    patch and footswitch layout -- "rig sheet" being ordinary guitar-pedal
    terminology for that kind of printout, not a shared codebase or a naming
    reference. Studio's own repo is GPL-3.0-only, a materially different
    license from the "all rights reserved" RigSheet project; this
    investigation's discipline (independently-derived protocol facts only,
    described in this project's own words, no copied code or text either
    way) was applied to it regardless, so nothing changes in how this
    project's own code or these notes were written either way.

15. **Resolved: this tool's own transport, run with Studio's exact strategy,
    is NOT stable -- and Studio's own source shows exactly why its export
    doesn't show that (2026-09-27, two real 256-slot `raw-sweep --all` runs
    back to back).** The user raised a sharp follow-up to finding 14:
    Studio's two export runs produced identical results with no
    content-verification at all, which is itself odd given this project's
    own measured ~12-15%-per-read glitch rate (finding 6) -- and the mere
    presence of ANY retry in Studio's code (even a timeout-only one) shows
    its author hit *some* kind of read trouble during development, whatever
    its exact shape. Comparing Studio's export determinism against this
    project's own read instability isn't quite apples-to-apples, though:
    Studio's "single read, retry only on timeout" is a different strategy
    running over a different transport (a browser's Web MIDI API) than this
    project's own host stack (python-rtmidi). Finding 14 could only show
    that Studio has no *mechanism* to catch a content glitch if one
    happened, not whether one actually would, on Studio's own transport.

    This project's own `read_dump` (RETRY_COUNT=1, i.e. exactly Studio's
    "one retry, timeout only, no content check" shape) already exists and
    is used by `reread`/`_confirm_discrepancy`, so the fair, cheap way to
    test this is on this project's OWN transport: run `read_dump` (the
    Studio-equivalent strategy) and `read_dump_confirmed` (this project's
    own trusted ground truth) back-to-back for the same slot and see how
    often they'd actually have disagreed. Added the `raw-sweep` command
    for exactly this (see Tools section below). Two things this can show
    once run on real hardware, twice in a row:
    - If the set of slots where `read_dump` disagrees with
      `read_dump_confirmed` is DIFFERENT between the two runs (expected,
      per every finding so far), that's independent host-side read noise,
      exactly consistent with finding 5/6 -- and would mean that on THIS
      transport, Studio's exact strategy would have produced different
      wrong answers on different days, its own two clean/identical runs
      most likely coming down to which specific bytes happened to glitch
      landing in fields Studio's UI/export doesn't surface, not to a clean
      transport.
    - If the mismatched-slot set is unexpectedly EMPTY or unexpectedly
      IDENTICAL across repeated runs, that would be new evidence worth
      taking seriously -- either that this rate is lower than finding 6
      measured, or that something more systematic than pure host noise is
      going on.

    **Real-hardware result.** Two full `raw-sweep --all` runs, back to
    back: run 1 flagged 28/256 slots (10.9%) where the raw (Studio-style)
    read disagreed with `read_dump_confirmed`; run 2 flagged 22/256 (8.6%).
    Both land right in finding 6/11's independently-modeled ~11-15%
    per-read glitch band -- the first direct, controlled (same session,
    raw vs. confirmed on the identical read) measurement of that rate at
    full 256-slot scale, rather than one inferred from confirmation-retry
    counts. Only 4 slots (24D, 38C, 48A, 48D) appear in BOTH runs' mismatch
    lists -- out of 28 and 22 flagged, i.e. 24/28 and 18/22 were NOT
    reproduced on the other run. That's close to what pure independent
    chance predicts (256 x 0.109 x 0.086 =~ 2.4 expected overlap by luck
    alone) and is strong, direct confirmation of the standing theory:
    transient host-receive noise, not a fixed set of trouble slots or a
    device-storage issue. Every single mismatch in both runs was confined
    to exactly two byte positions: file offset 0x0043 and 0x009F (the
    "pre-effects header byte 19" from finding 5/6) -- the two known
    trouble spots, and nothing new.

    **Why Studio's own export wouldn't show this, confirmed from its
    source (not inferred):** both trouble-spot offsets map, via Studio's
    own `CONTENT_FILE_START`-equivalent shift (dump index = file offset -
    0x28), to `decoded[27]` (file 0x43) and `decoded[119]` (file 0x9F) in
    `parsePresetFromDecoded`. Checked directly against the code:
    - `decoded[119]` is never read anywhere in that function at all -- it's
      the one unused byte sitting between the routing-order table
      (`decoded[108..118]`) and the first effect block (`decoded[120]`).
      A glitch there literally cannot reach anything Studio parses.
    - `decoded[27]` IS read, but only combined with `decoded[26]` into a
      16-bit value that's collapsed to a plain 0/1 `fxLoopMode` flag via an
      exact-equality-to-1 test (`(decoded[26] | (decoded[27] << 8)) === 1
      ? 1 : 0`). Any glitch value in the high byte other than exactly the
      one that produces 1 (vanishingly unlikely given the observed glitch
      values, e.g. 0xB6-0xB8/0x35-0x38) still collapses to 0 -- so the
      glitch is swallowed by the comparison, not filtered on purpose.

    And on the export side (`PRSTEncoder.encode`, checked directly): a
    device pull's parsed preset carries no `rawSource` buffer (`parsePresetFromDecoded`'s
    return value has no such field), so `hasRaw` is false on every "Export
    All" output. With `hasRaw` false, file offset 0x9F falls in a gap the
    encoder never explicitly writes (its routing-section writes cover only
    0x8C-0x8F and 0x94-0x9E), so it's left at `BufferGenerator`'s
    zero-initialized default -- always 0x00, regardless of what the device
    pull actually returned. File offset 0x43 (the high byte of
    `OFFSET_FX_MODE`, a `writeUint16LE` of the 0/1 `fxLoopMode` value) is
    always 0x00 too, for the same reason: a 0-or-1 value never sets its
    high byte.

    So the full chain is confirmed end to end: these two offsets glitch on
    this project's own transport at a rate matching finding 6's model; they
    would glitch identically on Studio's if its Web MIDI transport behaves
    like a typical MIDI stack (untested, but there's no reason to expect
    otherwise); but Studio's decode-into-typed-object-then-re-encode
    architecture pins both of them to a fixed 0x00 on every "Export All"
    output regardless of the raw bytes received, rather than reading and
    passing them through like this project's byte-copy export does. Its
    apparent determinism is a property of its export pipeline, not
    evidence its reads are clean -- exactly what finding 14 predicted, now
    demonstrated on real hardware with the exact mechanism identified in
    its own published source. This project's own retry protocol remains
    necessary for the same reason as always: unlike Studio's byte-copy-free
    re-encode, `build_prst_from_dump` preserves the raw dump verbatim onto
    the skeleton, so unfiltered read noise would otherwise reach the
    exported file untouched.

## Tools built this session for this investigation

- `upload` and `import` merged into a single `upload` command (2026-09-27).
  The two always did the same operation (write file(s) to slot(s)) differing
  only in cardinality and syntax (`upload file slot` vs. `import files...
  --start slot`) -- a single file was always just the N=1 case. `upload`
  now takes one or more `.prst` files, or a single `.zip`, followed by the
  DESTINATION SLOT as the last positional argument, always -- no `--start`
  flag, and the same shape whether it's one file or many:
  `upload patch.prst 34-B`, `upload a.prst b.prst c.prst 10-A`, `upload
  backup.zip 10-A`. `import` no longer exists as a separate command.
- Support for a single `.zip` in place of a long, typo-prone list of
  individual `.prst` paths (`expand_import_sources`, 2026-09-27, prompted
  directly by the first 48-slot `apply-template` bulk-scale run going well
  but making the command-line-length problem obvious). Order within the zip
  is taken from each entry's embedded slot label when this tool's own
  export naming (`37A_Template.prst`) is present on EVERY entry (numeric
  order, not alphabetical -- `8A` would otherwise sort after `37A` as plain
  text), or a plain alphabetical fallback otherwise (mixed labeled/unlabeled
  entries also fall back, all-or-nothing, rather than partially guessing).
  Deliberately does NOT let an embedded label pick the destination slot,
  only the order -- the trailing destination argument always decides where
  things actually land, so a zip is just as usable for reassigning patches
  to a brand new range as for anything else (the same way Valeton Desktop's
  own drag-and-drop slot reordering doesn't care where a patch used to be).
  Mixing a `.zip` with individual file arguments is refused rather than
  silently merged. Unit-tested (25 checks, including an end-to-end one
  proving the destination argument wins even when embedded labels point
  elsewhere, and one confirming the plain single-file/single-slot case still
  works exactly as `upload` always did); not yet run against real hardware.
  **Bug found and fixed same day**: the embedded-label parser
  (`_parse_leading_slot_label`) originally only recognized an underscore as
  the separator between slot label and patch name (this tool's own export
  convention, e.g. `37A_Template.prst`). A real user-supplied zip named the
  Valeton Desktop way instead -- a space separator with a dash inside the
  label itself (`36-A JImi.prst`, `34-B Lead.prst`) -- and every entry
  silently fell back to alphabetical ordering. In that particular zip
  alphabetical happened to still equal numeric order (all banks were
  two-digit, 34-36), so nothing would have gone visibly wrong, but a zip
  spanning e.g. bank 9 and bank 34 would have imported in the wrong order
  with no warning. Found by inspecting the zip programmatically before
  running it for real, rather than by a failure. Fixed by splitting on
  whitespace-or-underscore (`re.split(r"[\s_]", stem, maxsplit=1)[0]`)
  instead of underscore only; `label_to_slot` already stripped dashes, so no
  other change was needed. Added regression tests using the real filenames
  from that zip as fixtures, at both the label-parser level and the
  end-to-end `expand_import_sources` ordering level.
- **Bug found and fixed same day**: `export --all`'s overwrite-confirmation
  prompt used to only appear at the very end, after all 256 slots had
  already been read (a real run takes a couple of minutes). Declining, or a
  stray keystroke, threw away the entire read pass. Fixed by moving the
  check up front: the output filename for `--all` never depends on anything
  read from the device (it's either `--out` or the fixed default
  `gp200_all_patches.zip`), so it's now confirmed before opening the
  connection at all. (Single-slot `export`'s prompt still comes after its
  one read, since its default filename is built from the patch name that
  read returns -- not worth restructuring for a single read.) Regression
  test added confirming zero device reads happen when the prompt is
  declined.
- **Real-hardware round trip verified (2026-09-27)**: uploaded 5 patches via
  the new zip-based `upload` (34-A/B/C, 35-A, 36-A source labels -> 49-A
  through 50-A destination, in two separate real runs, one without
  `--force` where a mid-range slot was declined and correctly skipped, one
  with `--force`), then ran a full `export --all` (256 slots) and diffed the
  5 relevant exported files against the original source files
  byte-for-byte (ignoring only the documented dynamic offsets via
  `diff_prst_content`/`VERIFY_IGNORE_OFFSETS`, same as `normalize_export_dynamic_fields`
  applies). All 5 matched exactly -- upload -> device -> export is lossless
  end to end, not just individually tested in isolation. The same
  `export --all` run independently doubled as a full-device read-side
  stress test: 9 of 256 slots (12D, 19B, 23A, 25B, 27A, 31B, 38D, 54D, 55B)
  hit "3 reads never agreed" and were skipped (~3.5%, consistent with the
  known host-read glitch rate scaled up from the smaller 48-slot
  `apply-template` run) -- read-side only, none of the 5 newly-uploaded
  slots were affected, and a second export attempt on a skipped slot would
  be expected to succeed.
- `export --start/--end`: range export (2026-09-27), mirroring `apply-template`'s
  existing `--start`/`--end` range syntax and reusing its `parse_slot_range`
  helper. `export` now has three mutually exclusive modes -- a single slot,
  `--all` (all 256), or `--start`/`--end` (that inclusive range) -- all
  validated up front (exactly one required; `--start` and `--end` must be
  given together; a backwards range is rejected) before opening the device
  connection. Motivated directly by a real need in this same session: after
  a 5-slot `upload`, checking just those slots meant either a full 256-slot
  `export --all` (wasteful) or 5 separate single-slot `export` calls
  (clunky). Range mode reuses the same read-and-zip loop as `--all`, just
  over a smaller slot list, including the same up-front overwrite-prompt
  fix and a derived default filename (`gp200_<start>_to_<end>.zip`) when
  `--out` isn't given. Unit-tested (range reads exactly the given slots and
  no others, default filename, and all the mode-validation error cases);
  not yet run against real hardware.
- `calibrate-settle` — sweeps settle delay, tallies mismatch offsets.
- `reread` — repeated reads, NO writes, to isolate host-receive-side issues.
  Intentionally uses a raw, unconfirmed read every time (see finding 6).
- `soak` — fixed-delay N-cycle failure-rate measurement, for clean A/B
  comparisons (power supply, cable, computer, etc). Now also supports
  `--confirm-reads N` (extra no-write re-reads on any mismatch, to check
  whether the bad value is actually stored) and `--stop-on-failure` (halt
  right after that check instead of finishing the full run) — see
  finding 7.
- `diag-write` — the original bank-33+ (7-bit slot aliasing) addressing
  test; premise may be obsolete under the RigSheet model but kept as-is.
  Its pre-write backup read now uses `read_dump_confirmed` (finding 6).
- `Device.read_dump_confirmed()` — not a subcommand, but the core fix from
  finding 6: re-reads until two consecutive reads agree, used everywhere a
  read result needs to be trustworthy (write verification, export,
  diag-write's backup) rather than just fast.
- `normalize_export_dynamic_fields()` — export-only zeroing of the tail
  block AND the 0x2E slot-echo byte, so `export`'s output is reproducible
  and matches Valeton's own export behavior for both, without changing what
  write verification or read confirmation compare, and without touching the
  other two slot-mirror bytes (0x34, 0x90), which Valeton's own exports do
  NOT zero (finding 9).
- `raw-sweep` — new (2026-09-27, finding 15): for every slot in range
  (single slot, `--all`, or `--start`/`--end`, same range syntax as
  `export`), does one raw `read_dump` (Studio's own "single read, retry
  only on timeout" strategy) and one `read_dump_confirmed` (this project's
  own trusted ground truth) and reports where they disagree, using the same
  `raw_dumps_agree`/`_describe_raw_dump_diff` logic `read_dump_confirmed`
  already uses internally. Built to directly test whether this project's
  own transport would look as stable as GP200 Studio's export runs if run
  with Studio's exact read strategy -- run it twice and compare the
  mismatched-slot lists. Unit-tested (16 checks: all-clean, one mismatch,
  a raw timeout, an unconfirmable slot, single-slot/`--all`/range modes,
  and the mode-validation error cases); not yet run against real hardware.
- `--log-file PATH` — new (2026-09-27), start of the "productized version"
  pass. Redirects stdout AND stderr through a small `_Tee` for the rest of
  the process, so PATH ends up a complete, literal transcript of the run:
  an environment header (Python/OS/mido/rtmidi versions, MIDI ports seen,
  the exact command line, `SCRIPT_VERSION`), everything the run already
  prints at whatever `-d` verbosity was chosen, AND -- unlike the console --
  any `sys.exit(...)` error message or genuinely uncaught traceback, both of
  which are normally printed further up the call stack than this function's
  own frame. No new "what's worth logging" judgment calls: existing prints
  already encode that (most diagnostics -- `read_dump_confirmed`'s
  per-attempt disagreements, `print_failure_diagnostics` -- are
  unconditional; the raw per-message SysEx trace stays behind `-d`), so
  plain `--log-file` captures the former without the latter, and
  `--log-file -d` together captures everything.

  Two real bugs found and fixed while building this, both confirmed by
  direct end-to-end testing (not just unit tests) before shipping:
  1. The atexit cleanup originally closed the log file before restoring the
     real stdout/stderr. The interpreter's own final "flush stdout/stderr"
     step runs AFTER atexit hooks and would then call `.flush()` on a
     `_Tee` whose file half was already closed -- reproduced directly as an
     "unraisable exception" during interpreter teardown and the WRONG exit
     code (120 instead of 1). Fixed by restoring the real streams inside
     the atexit hook before closing the file, not after.
  2. `--log-file` was originally `nargs='?'` (an optional value, so a bare
     `--log-file` would auto-name the file). With a subcommand positional
     immediately after it, argparse's `nargs='?'` greedily consumes the
     NEXT token as the option's value regardless of intent: `--log-file
     list-ports` silently set `log_file='list-ports'` and left no
     subcommand at all, rather than running `list-ports` with logging on.
     Fixed by making the path a required value (plain `--log-file PATH`,
     no auto-naming) -- worse convenience, zero ambiguity; a bare
     `--log-file` now fails loudly with argparse's own "expected one
     argument" instead of silently misparsing.
  Verified end-to-end (real subprocess runs, not mocks) for: a normal
  successful command, a `sys.exit(...)` failure (bad slot label), and a
  genuinely uncaught exception (hit by accident in this sandbox, which has
  no real ALSA MIDI backend) -- all three land in the log file with exit
  code 1, not 120.

## Regression suite (2026-09-27)

Every `verify_*.py` script built ad hoc over the course of this
investigation (18 of them, one per feature/bugfix as it was built) only
ever existed in this session's own scratchpad -- never delivered, never
runnable by the user, and not collected anywhere. Per the user's explicit
call to establish "a standard and repeatable regression test suite" before
doing more productization work ("automated is better than manual... you
write the code once but you test it forever"), all of them are now real
project files:

- `tests/test_*.py` -- all 17 hardware-free scripts built over the course
  of this session (fake/mock `Device`s only, no real GP-200 needed),
  renamed from `verify_` to `test_` for the conventional prefix. Every one
  previously imported gp200.py via a hardcoded absolute path
  specific to this session's sandbox (`/home/claude/work/cli2/gp200.py`) and
  a few `os.chdir`'d to this session's scratchpad directory for their
  incidental file output -- neither would have worked at all on the user's
  own machine. Fixed throughout: gp200.py is now located relative to each
  test file's own path (`Path(__file__).resolve().parent.parent /
  "gp200.py"`), and every test that writes incidental files does so in its
  own `tempfile.mkdtemp()`, not a fixed path. Two spots that read
  `skeleton.prst` directly by hardcoded path were changed to call
  `gp200.resolve_skeleton_bytes(None)` instead -- strictly more robust, since
  that's portable AND works even if `skeleton.prst` isn't shipped alongside
  gp200.py at all (falls back to the embedded copy).
- `run_tests.py` -- new. Finds every `tests/test_*.py`, runs each as its
  own subprocess (so one test's crash or leaked state can't take another
  down), prints a PASS/FAIL line with timing, and on any failure shows the
  tail of that file's own output (its own PASS/FAIL lines plus whatever
  traceback caused the failure) so a failure is diagnosable without
  re-running it by hand. Exit code 0 only if every file passed. No
  registration step for a new test -- drop a `test_*.py` file into `tests/`
  following the existing pattern and the next run picks it up.

**Portability verified directly, not assumed:** copied `gp200.py`,
`skeleton.prst`, `run_tests.py` and `tests/` to a fresh directory outside
this project entirely (`/tmp/portability_check/some_other_folder_name`,
nothing else there) and ran `run_tests.py` from there cold -- all 17/17
passed, ~33s total (dominated by `test_calibrate_settle.py` at ~11s and
`test_soak.py` at ~15s, both of which sleep between simulated cycles even
against a fake device). Also deliberately broke one assertion
(`test_drain.py`) and re-ran to confirm the failure path itself works:
correctly reported 16/17, named the failing file, and printed enough of its
tail output (including the exact `AssertionError` traceback) to diagnose
without re-running by hand -- then reverted the deliberate breakage.

**A second, subtler litter bug found and fixed after the above (still
2026-09-27):** running the delivered bundle's `run_tests.py` from a genuine
clean room left behind a `__pycache__/gp200.cpython-311.pyc` AND three
`failed_verify_1A_<timestamp>.prst` files in the project root, even though
every test passed. Not assumed fixed at any point -- each fix below was
re-verified with a real before/after directory listing from a fresh
location, and the bug was NOT considered resolved until that diff came back
empty.

- The `.pyc` was straightforward: every test imports `gp200.py` via
  `importlib`, which writes a compiled cache next to it like any normal
  Python import. Fixed centrally in `run_tests.py` by setting
  `PYTHONDONTWRITEBYTECODE=1` in the subprocess environment for every test
  file, rather than patching each test individually.
- The `.prst` files took two rounds to actually run down. `test_soak.py`
  drives `cmd_soak` through deliberate write-failure and mismatch scenarios
  with no mocking of the diagnostics path, so it hits the real
  `print_failure_diagnostics` -> `_save_failed_readback` code and writes an
  actual `.prst` to whatever the current directory happens to be -- unlike
  the other tests that reach failure paths, it had never been given the
  `tempfile.mkdtemp()` + `os.chdir()` isolation the others already had.
  Fixed, re-verified from a fresh clean-room copy -- and the SAME three
  `failed_verify_1A_*.prst` files still appeared. `test_soak.py` was a
  source, not the source.
  The actual remaining culprit, found by grepping `gp200.py` itself for
  every `_save_failed_readback` call site rather than continuing to guess
  from the test side: `cmd_calibrate_settle` calls it directly on its
  give-up path (distinct from the `print_failure_diagnostics` route the
  other commands use), and `test_calibrate_settle.py` -- which deliberately
  drives an always-bad fake device to that exact give-up path, using slot
  `"1-A"`, in three of its six scenarios -- had no chdir isolation at all.
  Fixed the same way (own `tempfile.mkdtemp()`, `os.chdir()`,
  `atexit`-registered cleanup).
- Re-verified clean twice over from a brand new clean-room copy
  (`/tmp/litter_check3`, nothing else there): 17/17 passed both times, and
  a `find`-based before/after file listing diffed to nothing -- not "no
  `.prst` files noticed," but zero new files of any kind, checked by
  comparing the full directory tree, and re-checked on a second consecutive
  run to rule out first-run-only effects (e.g. a cache warm-up).

Going forward: run `run_tests.py` before and after any change, per the
user's stated approach. Every new feature this session added mid-session
(`export --start/--end`, `raw-sweep`, `--log-file`) already had its own
`verify_*`/`test_*` script built alongside it in the same turn, so this
suite already covers everything currently in `gp200.py` -- it's a
consolidation and delivery step, not a backfill of missing coverage.

## Open items as of 2026-09-27

- ~~Power-supply A/B test~~ — DONE, ruled out (see finding 4).
- ~~`reread` on a slot with a known-corrupted byte, no writes~~ — DONE,
  positive: confirmed a real host-side receive glitch (see finding 5).
- ~~`soak --confirm-reads` / `--stop-on-failure` real-hardware run~~ — DONE,
  3 runs (2026-09-27): 3/60 mismatches (5% each run), 0 ever STORED (see
  finding 7). This also incidentally answers the "re-run plain soak to see
  if the rate drops" item below, since `--confirm-reads` runs sit on top of
  the same `read_dump_confirmed`-backed `soak`. Not airtight proof writes
  never fail, but a real, repeated, consistent result in that direction —
  worth revisiting only if a future run ever does report STORED.
- Second-computer test (same calibrate-settle/soak sequence on a different
  machine) — not yet run, and now lower priority given finding 5 already
  strongly implicates the host MIDI stack in general (not this specific
  machine). Would still help separate host-side from device-side causes:
  same offset AND same value range on different hardware/OS points
  strongly at the device; a different pattern or no failures points at the
  host/transport.
- Whether Active Sensing byte interleaving (the `thestk/rtmidi#387`
  mechanism, distinct from buffer sizing) plays any role specifically —
  still untested at the mechanism level; `read_dump_confirmed` sidesteps
  needing to know this by requiring read-to-read agreement regardless of
  cause, but it would still be good to understand for the manual.
- Whether the official Valeton Desktop software itself ever shows a
  visible retry/warning when a save doesn't stick, or fails completely
  silently — unknown, user doesn't recall paying attention at the time.
- ~~Export comparability with Valeton's own exports~~ — DONE (finding 9):
  `export` now zeroes the tail block AND the 0x2E slot-echo byte, confirmed
  against two real Valeton Desktop exports of the same patch. After this
  fix, the only remaining differences against a real Valeton export are the
  already-understood, unavoidable ones (the dynamic 0x1C-0x1F header region,
  which even Valeton's own tool doesn't reproduce export-to-export, and the
  Valeton-only 0x28-0x2D stamp).
- Whether the device actually requires/accepts a particular value at 0x2E on
  WRITE, or whether `build_upload_image`'s "ordinary content, not
  recomputed" characterization needs updating — flagged as an open question
  in that function's docstring (2026-09-27), not yet tested. Finding 9 only
  tested the export/read side.
- ~~Second-patch comparison: Valeton-exported vs. our-tool-exported `.prst`
  for a DIFFERENT patch than 37-A "Blue Sparkle"~~ — DONE (2026-09-27): 18-D
  "Demo Shimmer", 0 unexpected differences (see finding 9's update). Fix
  confirmed to generalize beyond the one patch/slot it was found on.
- Findings 5–7 belong prominently in section 3 of the eventual manual, not
  as a footnote — "the write path is fine, the read-verification path
  wasn't" is a materially different and more useful story for anyone doing
  real bulk backup/restore work than "writes are occasionally flaky."
- ~~Re-run `export` and `calibrate-settle` against the `raw_dumps_agree` fix
  from finding 8~~ — DONE, same day (2026-09-27): both clean on real
  hardware (`export` confirmed in 2 reads; `calibrate-settle` 20/20 at the
  first settle delay, zero mismatches). See finding 8's update.
- Still open: the exact update timing/mechanism of the tail block (why the
  OLD, equally-unfiltered `read_dump_confirmed` mostly succeeded in
  finding 7's three runs rather than failing constantly like it did once
  `calibrate-settle` exercised it harder) — not blocking, not fully
  understood, noted in finding 8.

## Finding 10: non-ASCII patch names (2026-09-28)

Raised as a concern before any public sharing of the tool: the GP-200 ships
in multiple markets, including a Chinese-firmware version, and it's not
known whether patch names entered on that hardware (or via a companion app)
could contain non-ASCII bytes in the 16-byte name field. Investigated by
reading the actual code paths rather than assuming either "it's fine" or
"it's broken":

- **The exported `.prst` file's name bytes are never at risk.**
  `build_prst_from_dump()` writes an exported file by overlaying the raw
  device dump directly onto a skeleton — it takes a `name` string parameter
  but never actually uses it for the file's byte content. Whatever bytes the
  device sent for the name field land in the output file completely
  unchanged, regardless of what any Python string manipulation does to a
  derived display name. Verified with a real (not assumed) test:
  `tests/test_export_name_encoding.py` builds a dump with non-ASCII name
  bytes standing in for an unknown encoding (UTF-8-shaped, since we don't
  know what a real Chinese-firmware unit would send) and confirms the exact
  same bytes land at file offset 0x44 in the written `.prst`/zip entry.
- **The display name (console output, derived export filename) was a real,
  if cosmetic, bug.** `cmd_export`'s two inline extraction sites did
  `"".join(chr(b) for b in decoded[28:44] if b)` — a byte-for-byte Latin-1
  mapping. For any byte ≥ 0x80 this produces a *confident-looking but wrong*
  character (e.g. a UTF-8-encoded CJK name would come out as scrambled
  accented-Latin letters), which is worse than an obvious placeholder
  because it doesn't look broken. It was also inconsistent with
  `parse_preset_name` (used by `list`/`read`), which already decoded safely
  via `.decode("ascii", "replace")`.
- **Fix applied**: extracted the shared logic into one function,
  `extract_name_field()`, used by `parse_preset_name` and both `cmd_export`
  call sites. It decodes as ASCII with Python's standard `"replace"` error
  handler, which substitutes the Unicode replacement character (U+FFFD, "�")
  for anything non-ASCII — an honest "this wasn't ASCII" signal instead of
  invented letters. This only changes what gets printed to the console and
  what filename `safe_filename()` derives; it has zero effect on the actual
  `.prst`/zip file content (see above). `safe_filename()` itself was checked
  too: it only strips filesystem-reserved characters (`\/:*?"<>|`) and
  doesn't choke on non-ASCII code points, including U+FFFD, on any platform
  tested against.
- **Still genuinely unknown, and not fixable from here**: what character set
  (if any) the GP-200's own name field actually supports for non-ASCII
  entry — ASCII-only via the hardware's own input method, some vendor
  double-byte encoding for the Chinese firmware, UTF-8, or something else
  entirely. Nothing in this project has ever touched a non-English-market
  unit or a companion app that writes exotic bytes into that field. If
  someone with a Chinese-firmware GP-200 (or any patch name typed with
  accented/non-English characters) reports back, that's real evidence this
  file should capture — until then this stays an open item, not a settled
  one.

Net effect for the planned public post: no patch data is ever at risk from
this, on any firmware/language — the fix only makes the tool's own console
output and generated filenames honest instead of silently wrong for a name
it can't fully understand.

## Finding 11: patches reference User-IR/NAM slots by index, not by content (2026-09-28)

Prompted by a real question from a Facebook group member after the
project's first public post: the GP-200 lets you load your own impulse
response (IR) and NAM (Neural Amp Modeler) captures onto the device, then
pick them like any other cab/amp/drive option when building a patch. Does
`export`/`upload` carry that sound along with the patch, on the same device
and across two different devices? Investigated by reading (not copying) the
GPL-3.0 GP200 Studio source and the independently reverse-engineered
RigSheet effect-name table, both already credited above as protocol
sources — not by any new hardware capture.

- **A patch stores a plain numeric selector for each effect slot, never the
  underlying audio/model data.** Each of a patch's 11 effect blocks (72
  bytes each, starting at file offset 0xA0) has a 4-byte little-endian
  "model code" field at offset 8 within the block. GP200 Studio's own
  decoder reads this field directly (`MODEL_OFFSET = 8`) and looks it up in
  a fixed table of ~300 known codes to get a display name — there is no
  separate field anywhere in the 1224-byte file for audio samples or
  network weights, and the file size is constant regardless of what's
  selected. This confirms the "pointer to a slot" model directly rather
  than by inference from file size alone.
- **User-loaded IRs are addressed the same way as any built-in cab.**
  GP200 Studio's effect-code table has a dedicated run of codes
  `0x0A100000`–`0x0A10001D` (30 entries) under its "CAB" module, every one
  named generically "User IR" — i.e. codes that mean "whatever the user
  loaded into User-IR slot N," not a specific sound. A comment in GP200
  Studio's own connect sequence independently confirms the device exposes
  "30 User-IR slot names" via its own SysEx query.
- **NAM captures work the same way, under a different name.** GP200
  Studio's table has no entries for these, but RigSheet's independently
  reverse-engineered effect table does: codes `0x0F000000`–`0x0F000004` (5
  slots) tagged module "AMP" and `0x0F000005`–`0x0F000009` (5 slots) tagged
  module "DST", every one named "SnapTone" with the description "For
  importing and using the .nam file." So a captured NAM profile is
  addressed by the same kind of small numeric index, just in the amp and
  distortion module slots instead of the cab slot, and RigSheet's slot
  labels ("empty 1".."empty 4") show it treats them exactly like an
  otherwise-unassigned slot until the user loads something into it.
- **Neither reference project moves the actual IR/NAM content on
  export, either.** GP200 Studio's own patch-export flow
  (`ExportPresetDialog` → `PRSTEncoder`) only ever writes name, author and
  target slot into the standard .prst layout — it does not read or bundle
  whatever audio/model data a referenced User-IR/SnapTone slot currently
  holds. GP200 Studio does separately query the 30 User-IR slot *names*
  (a distinct SysEx exchange, `0x12`/`0x1C` "assignment query" messages,
  not the `0x10`/`0x12`/`0x18` dump/read-name messages this project already
  uses) purely so its own effect picker can show what's loaded — but that's
  labels only, not the binary content, and no equivalent query for SnapTone
  slot names or content was found anywhere in either reference project.

**What this means for `export`/`upload` as they exist today:**

- *Same slot content, same or different device:* a patch that references
  User-IR slot 7 (say) will sound right after export+reimport as long as
  slot 7 still holds the same IR on whichever device it's imported to —
  because `export`/`upload` already move the whole effect-block region
  byte-for-byte, unmodified, including that 4-byte selector. No fix needed
  here; this was already correct, just previously unverified.
- *Different content in that slot (most likely on a different device, but
  also possible on the same device after the user swaps what's loaded into
  a slot):* the patch will silently select whatever is now in slot 7
  instead — a different sound, or nothing at all if that slot is empty. The
  .prst format has no way to detect or flag this; it is a structural limit
  of "select by index," not a bug specific to this tool. GP200 Studio's own
  patch files have the identical exposure.
- This is exactly the pattern already established for slot-position
  metadata elsewhere in this file: the pointer is faithfully preserved,
  but what it points to lives outside the patch and outside this tool's
  knowledge.
- **Manually restoring the IR/NAM files themselves isn't enough on its
  own, either.** The selector is a slot *number*, not an identity or a
  filename, so a patch that references User-IR slot 7 will only sound
  right again if whatever gets (re)loaded ends up in slot 7 specifically --
  reloading the exact same file into a different slot breaks the patch
  exactly as much as a missing or swapped one would. (Confirmed by a real
  Facebook-group report the same day this was written up, after this
  project's author posted a PSA about the limitation above.)

**Open, not yet investigated:** the actual binary format and SysEx
transfer for User-IR/SnapTone slot *content* (as opposed to the numeric
selector or the name) is undocumented in all three of this project's own
notes, GP200 Studio, and RigSheet. Building real IR/NAM export/import
would need new reverse-engineering (most likely USB capture of the
official Valeton editor loading a .wav/.nam file), not just reading
existing open-source references. A smaller, immediately buildable step
that needs no new reverse-engineering: teach `export`/`list` to decode the
model code in each effect block and print when a patch depends on a
User-IR or SnapTone slot (and which index), using the code ranges
confirmed above — surfacing the dependency even before this tool can move
the referenced content itself.

## In progress: why does `list` feel slower than `export --all`? (2026-09-29)

Real-world observation, not yet explained: `list` (256 single-chunk
name-only reads) is reported as noticeably slower in practice than
`export --all` (256 confirmed *multi*-chunk full-dump reads, each needing
2+ round trips to agree) -- backwards from what the amount of data/protocol
work per slot would predict. Compared against GP200 Studio's own
name-loading code (`loadPresetNames`), which uses the identical name-only
request (`sub=0x20`/`0x18`) with a 250ms timeout and a comment noting the
device "responds in ~20ms normally" -- there's no evidence either tool has
some other, faster bulk-list message being missed; the mechanism is already
the same one both projects use. So the likely difference is in per-request
timing/timeout behavior, not the message shape, but that's a hypothesis,
not yet a finding.

**Instrumentation added to test this** (no protocol/behavior changes,
verified by the existing regression suite plus a new
`tests/test_debug_timing.py`):
- Every `--debug` line is now prefixed with elapsed time since the device
  connected (`Device._dbg`, timed from `Device._t0`) -- previously there
  were no timestamps anywhere in the trace, so seeing *how long* a read
  took meant eyeballing raw message dumps with no timing in them at all.
- `_drain_matching` now reports the actual measured round-trip time on a
  successful read (`received in X.XXXs`), and on a timeout, the real
  elapsed time rather than just echoing back the configured timeout budget
  (`timed out after X.XXXs of Y.Ys budget`).
- `list` and `export`'s batch path (`--all`/`--start`/`--end`) each now
  print an unconditional one-line total (slot count, elapsed seconds, and
  for `list`, a timeout count) even without `--debug` -- cheap enough to
  always show, and it directly answers "how long did this actually take"
  without needing a full trace.

**Next step**: run `list -d` and `export --all -d` back to back on real
hardware and compare. If `list`'s reads are hitting the 2.0s
`READ_TIMEOUT_S` budget on a meaningful fraction of slots while
`export`'s full-dump reads mostly don't, that points at giving name-only
reads their own much shorter timeout (matching GP200 Studio's 250ms,
justified by the same "single self-contained chunk, no reassembly
ambiguity" reasoning that already applies to `read_name` vs `read_dump`).
If instead both commands show prompt replies per-slot, the slowness isn't
timeout-related at all and needs a different explanation. Either way,
nothing here is fixed yet -- this section exists so the eventual real
numbers land next to the hypothesis they're testing, not lost in chat.

**Resolved (2026-09-29), with real-hardware evidence.** Two full-256-slot
traces on the actual device (`list -d`, `export --all -d`) confirmed the
first half of the hypothesis exactly, with zero exceptions in either
direction:
- Every single name-only (`sub=0x20`) request's *first* attempt timed out
  the full 2.0s `READ_TIMEOUT_S` budget, succeeding only on the automatic
  retry roughly 15-16ms later. 256 slots, 256 full timeouts, no
  exceptions.
- Every full-dump (`sub=0x10`) request -- the same one `read_dump`/`export`
  already use -- succeeded on its *first* attempt in under 20ms. 256
  slots, zero timeouts.

So the timing problem was real, request-type-specific, and reproduced
perfectly -- but the fix taken wasn't "give name-only reads a shorter
timeout" (the originally planned next step above). A shorter timeout would
still pay that timeout on every slot, just a smaller one; it doesn't
explain *why* `sub=0x20`'s first attempt fails at all, and chasing that
root cause further wasn't judged worth it against a simpler fix that
sidesteps the bad request type entirely (this project's stated preference
throughout: provably-correct simple fixes over unverified heuristics).
Instead, `list` was switched to a new `Device.read_name_via_dump()`, which
just calls the already-proven-fast `read_dump()` and keeps only the name,
discarding the rest. Confirmed by the user on real hardware: "The whole
list was printed in 3 seconds" (down from roughly 8.5 minutes).

Deliberately scoped narrow: `read_name`'s other call sites (`cmd_read`,
`confirm_overwrite`, `verify_write`, `diag_write`'s two calls) were left
using the old name-only request, since none of those were part of what was
slow or what was asked to be fixed. Switching them to the same approach is
an easy, low-risk follow-up if `read_name`'s slow-first-attempt behavior
ever matters somewhere else, but wasn't done here.

*Still genuinely open, deprioritized:* **why** `sub=0x20`'s first attempt
reliably fails while `sub=0x10`'s doesn't. Also noticed but not chased: a
"6 pending messages flushed" count that showed up consistently on every
slot in the `list -d` trace, unexplained. Neither is blocking anything.

## `export` warns about patches that depend on a User-IR/SnapTone slot (2026-09-29)

Direct follow-up to Finding 11's closing paragraph, once the `list` speed
fix above was confirmed: "teach `export`/`list` to decode the model code
in each effect block and print when a patch depends on a User-IR or
SnapTone slot." This doesn't move any IR/NAM content (still out of scope,
per Finding 11 -- no GP-200 tool does that) and doesn't change what gets
written to the `.prst`/zip; it's purely an export-time heads-up so the
dependency is visible right when it's still actionable, not discovered
later as an unexplained sound difference.

`find_ir_nam_dependencies(decoded)` scans a decoded dump's 11 effect
blocks (same block layout Finding 11 already established: 72-byte blocks
starting at decoded-dump offset 0x78 -- file offset 0xA0 shifted -0x28 --
4-byte LE model code at block offset 8) and, for each block whose model
code falls in the User-IR (`0x0A100000`-`0x0A10001D`) or SnapTone
(`0x0F000000`-`0x0F000004` amp-position, `0x0F000005`-`0x0F000009`
drive-position) ranges, reports which slot it points at.

`cmd_export` calls this for every patch it reads and, when a patch has any
such dependency:
- **Single-slot export**: prints an inline `NOTE:` line right after that
  patch's normal export line, naming what it depends on.
- **Batch export** (`--all`/`--start`/`--end`): collects findings across
  the whole run and prints ONE end-of-run summary block naming each
  affected slot and its dependency, mirroring the existing
  `skipped_labels` gap-warning's pattern (collect during the loop, one
  clear block at the end) rather than interleaving a note per slot into
  256 lines of otherwise-routine output.

A clean patch with no such dependency (the common case) produces no extra
output at all in either mode. Covered by
`tests/test_ir_nam_dependency_warning.py`: the code-range boundaries in
`describe_ir_nam_dependency`, `find_ir_nam_dependencies` scanning a
realistic multi-block dump (including that a short/malformed dump is
skipped rather than raising, since this is informational and not
something export's correctness depends on), and both the single-slot and
batch `cmd_export` output paths, in both the dependent and clean cases.

**Amended same day, before this had been tested on real hardware**, per
two direct corrections:

1. *"there are snaptone slots in both the amp module and the dist
   module... our warning should clarify which."* The AMP-position and
   DST-position code ranges both address the same 5 physical NAM-capture
   slots, but a block using the AMP-position code and a block using the
   DST-position code are two distinct, independently-loadable uses of that
   capture (as the amp vs. as the drive/distortion) -- not the same thing
   twice. The original description collapsed both into "SnapTone (NAM)
   slot N" with no way to tell which position was meant.
   `describe_ir_nam_dependency` now tags each one: `"SnapTone (NAM) slot N
   (amp)"` or `"SnapTone (NAM) slot N (dist)"`.
2. *"it is perfectly possible that one patch uses both as well as an IR.
   So the warning should ensure all are mentioned."* This was already true
   of `find_ir_nam_dependencies`' implementation (it scans all 11 blocks
   and appends every match, not just the first), but it hadn't been
   explicitly tested as a guarantee -- only single-dependency patches were
   covered. Added a test scanning a patch with a User-IR reference AND a
   SnapTone-as-amp reference AND a SnapTone-as-dist reference on the SAME
   underlying slot number all at once, confirming all four (the three
   plus a second SnapTone-dist on a different slot) come back distinctly
   and in chain-block order -- run both at the unit level
   (`find_ir_nam_dependencies` directly) and end-to-end through
   `cmd_export`'s single-slot path, so the guarantee holds all the way to
   what actually gets printed, not just in the scanning function.

One internal-naming note for future readers of `gp200.py`: this feature's
constants are named `DUMP_EFFECT_BLOCK_START`/`DUMP_EFFECT_BLOCK_SIZE`
(decoded-dump-offset-based, 0x78), deliberately prefixed to avoid confusion
with two unrelated, pre-existing *local* variables of the same un-prefixed
names elsewhere in the file (`prst_to_preset`'s `.prst`-file decoder, and
the raw-dump-diff offset namer) that use the FILE-offset layout (0xA0) for
the same-shaped 11×72-byte blocks. Same shape, different base offset,
easy to confuse -- hence `DUMP_`.

## Two console-noise sources gated behind `--debug` (2026-09-29)

Direct feedback after the first real end-to-end test of the IR/NAM warning
above (which itself worked correctly -- a patch built to reference a
User-IR AND both SnapTone positions at once was reported with all three,
correctly labeled): "I also saw a lot of extra output that should probably
be suppressed unless -d is used," from a real `export --all` console (256
slots, 6.8s). Two sources, both previously unconditional:

1. **`build_prst_from_dump`'s overlay-accounting line** (`"(overlaid N of M
   dump bytes onto the skeleton...)"`) printed on every single call --
   once per slot on a batch export, so 256 identical-shaped lines on a full
   `export --all` even when every single byte accounted for exactly as
   expected. Gained a `debug: bool = False` parameter; every call site
   (`verify_write_full`, both of `cmd_export`'s paths, `cmd_diag_write`'s
   backup, `cmd_reread`, `_confirm_discrepancy`) now threads its own
   already-available debug flag (`self.debug`, `args.debug`, or `dev.debug`
   depending on the call site) through explicitly, since this is a free
   function with no `self` of its own.
2. **`read_dump_confirmed`'s retry-diagnostic prints** (the "read N vs read
   M: ... differ" and "read N matches read M -- confirmed after N
   attempt(s)" lines). These were DELIBERATELY unconditional when first
   written (see the method's docstring, prior version): the reasoning was
   that a disagreeing read is exactly the situation that's hard to reason
   about blind, so hiding it behind --debug felt like hiding the one thing
   worth seeing. In practice, at the scale of a real 256-slot run, that
   reasoning produced real noise instead: the independently-measured
   ~12-15%-per-read glitch rate (see the `reread` command finding this
   file already documents) means a meaningful fraction of slots print 2+
   of these lines apiece even on a fully successful export with nothing
   actually wrong. Switched the two `print()` calls to `self._dbg()`,
   which is already gated on `self.debug` (the same helper the
   elapsed-timestamp debug instrumentation above uses) -- the detail is
   still fully available, just behind `--debug` now like everything else
   diagnostic, rather than being the one exception to that rule.

Both are one-line, low-risk changes in isolation, but every existing test
that exercises the REAL `Device.read_dump_confirmed` or
`Device.verify_write_full` against a hand-written fake (rather than faking
those methods out entirely) needed that fake given a `.debug` attribute
(and, where a real retry-diagnostic could actually fire, `._dbg`/`._t0`
too) -- `self.debug` is now unconditionally read by `verify_write_full`
regardless of whether anything ends up printing, since it's passed through
as `build_prst_from_dump`'s `debug=` argument either way. Fixed in
`test_diag_write.py`, `test_diagnostics_and_reread.py`, and
`test_export_confirmed.py` (`test_soak.py` similarly needed `self.debug`
added to `FakeDevBase`, for `_confirm_discrepancy`'s call). New coverage:
`test_gp200.py` gained direct checks that `build_prst_from_dump` is quiet
by default and verbose with `debug=True`; `test_read_dump_confirmed.py`
gained the mirror image for the retry-diagnostic lines (including an
explicit assertion that a diff-and-recover scenario prints NOTHING with
debug off, which is the actual behavior change, not just a side effect);
and a new `test_export_quiet_output.py` covers both mechanisms together,
end-to-end through `cmd_export` itself, matching the shape of the real bug
report rather than trusting that the two pieces being individually correct
guarantees the combination is.

## Round 2 of export console noise (same day, next real test)

The IR/NAM dependency warning worked correctly on its first real test --
a patch built to reference a User-IR and both SnapTone positions at once
was reported with all three, correctly labeled. But the same run surfaced
three more things left in `export`'s output that only make sense to
someone reading this file, not to the guitar player actually running the
command:

1. *"I don't see much value in spitting out the name of every patch. The
   summary at the end should be enough."* The per-slot progress line
   (`"{label}: {name!r} ({bytes} bytes)"`) -- printed on both the
   single-slot and batch export paths -- moved behind `--debug` entirely.
   Nothing meaningful was lost: the single-slot path's final `"Wrote
   {out_path}"` line already names the patch (it's baked into the output
   filename), and the batch path already has its own end-of-run summary
   (`"Wrote N patches to X (N slots read in Ys)"`).
2. *"the message for a skipped one about 5 consecutive reads not matching
   is a debug thing - meaningless to a guitar player."* A skipped or
   failed slot used to show the raw `ReadNotConfirmedError` text verbatim
   -- `"5 read(s) of 34B never agreed with each other -- couldn't get a
   trustworthy read"` -- which names an internal retry count (the
   `tries` default) that reads as a bug report, not a status update. New
   `describe_read_failure(e)` gives a plain-language reason instead:
   `"couldn't get a reliable read (the device's answers didn't agree)"`
   for a `ReadNotConfirmedError`, `"no response from the device"` for a
   plain timeout -- keeping that one real distinction (dead silence vs. a
   noisy connection) without the retry-count detail. The raw exception
   text is still appended in parentheses when `--debug` is on.
3. *"the reference to the PROTOCOL NOTES file can be dropped. I do not see
   that as a document a user would ever want or need to refer to - that is
   more for the benefit of anyone trying to write software."* Fair --
   this file is exactly that: reverse-engineering notes toward a future
   protocol document, not user-facing help. The IR/NAM dependency NOTE (in
   both export paths) dropped its `"See PROTOCOL_NOTES.md (Finding 11)"`
   half and now only points at the README's "Known limitations" section,
   which IS written for a user.

None of these needed protocol changes, only output wording -- but several
existing tests exercise the REAL `Device.read_dump_confirmed` or
`Device.verify_write_full` against hand-written fakes (see the "console-
noise sources" entry above for why those needed a `.debug` attribute in
the first place), and one (`test_export_confirmed.py`) asserted on the
literal old wording ("never agreed" with no `--debug`), which needed
updating to match the new plain-language default. New/updated coverage:
`test_export_quiet_output.py` gained checks that the per-slot line is
gone by default and back with `--debug`, plus a new never-agrees scenario
proving the friendly message by default and the raw detail with
`--debug`; `test_ir_nam_dependency_warning.py` gained explicit checks
that neither export path's NOTE mentions `PROTOCOL_NOTES` and that both
still point at the README.

**Still outstanding, not yet addressed**: the user mentioned this most
recent real run had "a few failures" and is sending the console log
separately -- not yet reviewed as of this writeup.

## `read_dump_confirmed` reliability: 5 tries wasn't enough either (2026-09-29)

The "a few failures" mentioned above turned out to be concrete: a real
255-slot run hit `read_dump_confirmed`'s `ReadNotConfirmedError` ("never
agreed") on 3 slots (~1.2%) at `tries=5`. That's notably worse than the
model in `read_dump_confirmed`'s own docstring predicts for `tries=5`:
with the independently-measured ~12-15%-per-read glitch rate, `P(<=1
clean read out of 5)` works out to roughly 0.1-0.2%, i.e. ~0.3-0.5
expected failures across 255-256 slots, not 3. A Poisson check against
that expected rate puts `P(observing >=3)` at only about 1-2% -- rare
enough to treat as a real signal, not so rare that it rules out "the
model is directionally right but the true glitch rate runs a bit hotter
in practice than the earlier measurement."

Rather than bet on one explanation, two independent, low-risk changes
went in together, because they each address a different plausible cause
and neither costs anything on the common (fast-confirming) path:

1. **`tries` raised 5 -> 7.** If the glitches really are independent
   per-read noise, this helps a lot on its own: the dominant "never
   agree" term (needing `tries - 1` bad reads before a single clean one)
   falls off fast with `tries`, so 7 should drop the model's predicted
   rate from ~0.1-0.2% to roughly 0.01% -- back near "shouldn't ever
   realistically see it at 256-slot scale."
2. **Graduated delay before the later attempts.** If some glitches
   instead come in correlated bursts -- e.g. the host MIDI stack still
   catching up from back-to-back requests, already the leading theory
   for the unrelated `sub=0x20` first-attempt-always-times-out mystery
   elsewhere in this file -- then more *immediate* retries wouldn't help
   much, since a burst can span several consecutive reads regardless of
   how many are attempted. `read_dump_confirmed` now pauses 250ms before
   attempt 4, and 500ms before every attempt after that, matching the
   shape suggested directly: *"wait 250 msec after read 3 and if it
   still fails, wait 500msec after read 4."* This only fires on the
   small tail of slots that need more than 3 attempts, so the large
   majority of slots (which confirm in 2) are completely unaffected.

Both changes are cheap, reversible tuning, not a claim about which
theory is correct. The existing `--debug` diagnostics (`self._dbg()`
calls inside `read_dump_confirmed`, already gated per the entry above)
are left in place specifically so the *next* real run with `--debug` can
distinguish the two theories: if failing slots now confirm on attempt
4+ (after a pause), that's evidence for the correlated/timing theory; if
some still never agree even after 7 tries and all the pauses, that
points at something other than transient read noise entirely (or that
the per-read glitch rate is simply higher than the ~12-15% measured
earlier and needs re-measuring).

Tests: `test_read_dump_confirmed.py`'s default-tries coverage was
updated from 5 to 7 attempts (both the confirms-on-the-last-try and
never-agrees cases), plus new checks that the delay schedule is exactly
`[0.25, 0.5, 0.5, 0.5]` before attempts 4-7, that a slot confirming
within 3 tries never sleeps at all, and that the pause itself shows up
in `--debug` output. `time.sleep` is patched to a no-op (recording what
it was asked to sleep for) in both `test_read_dump_confirmed.py` and
`test_export_quiet_output.py` so these checks run instantly instead of
costing several real seconds per test.

## `list`: a failed slot was easy to miss (2026-09-29)

Direct question: *"does list output print an error if a slot could not
be read or just omit it? I think it should say 'Error reading patch
<patch number>'."* Verified: `list` never omits a slot -- every slot's
label always prints -- but a failed read's name column just showed
`"(no response)"`, which blends in easily when skimming 256 lines and
could in principle be mistaken for a patch that's genuinely named that.

Changed the placeholder to `f"*** error reading this patch: {describe_read_failure(e)} ***"`,
reusing the same plain-language phrasing `export` already uses for the
identical underlying failure (`describe_read_failure`, added earlier in
this file's history) rather than inventing new wording -- so a failure
reads the same way whether it shows up in `list` or `export`. Slightly
different from the user's exact suggested text (which would have
repeated the slot number redundantly, since the label already prints in
the column to its left), but keeps the spirit: unmistakable, plain
language, not a debug dump.

Note: `read_name_via_dump` (what `list` actually calls) uses a bare
`read_dump`, not `read_dump_confirmed` -- see that method's own
docstring for why (a wrong name once in a while is cosmetic). So the
only failure `list` can report here is a genuine dead-silence timeout,
never a `ReadNotConfirmedError`; `describe_read_failure` already handles
that correctly since it distinguishes the two.

Tests: `test_debug_timing.py`'s existing `cmd_list` timeout-counting
scenario gained checks that the failed slot's label still prints and
that its name column carries the new unmistakable error text.

## The 7/256 `export --all` failure run: `reread` isolates it to two bytes, cables muddy the cause (2026-09-29)

The "a few failures" run finally showed up, and it was worse than the
3/255 that prompted the tries=5->7 + delay change above: 7/256 slots
(23B, 25B, 29C, 46C, 47B, 55A, 55D) failed to confirm even at tries=7.
Wall time (69.1s for the read loop) was also notably higher than `list`'s
~3s for the same 256 slots -- expected in part (every slot here pays for
a confirmed multi-read, `list` pays for one unconfirmed read; and the
7 outright failures each burn the full 250/500/500/500ms delay ladder,
~12s of the 69s on their own), but not fully explained by that alone,
suggesting the run itself was noisier than usual, not just slower by
design.

**`reread --count 15 --debug` on 5 of the 7 failed slots (23B, 25B, 29C,
46C, 47B -- 55A/55D were deleted before upload and not re-captured) turned
up something the whole "per-read glitch, retry until two agree" model had
missed: every single disagreement, across all 5 slots x 15 reads = 75
read-pairs, landed on exactly one or both of two file offsets -- 0x0043
and 0x009F -- and NEVER anywhere else in the other ~1174 bytes of the
dump.** 0x009F was already a known, previously-flagged finding (see
`describe_prst_offset`'s comment on the 0x8C-0xA0 range, from earlier
`calibrate-settle` testing). 0x0043 was not previously named or
flagged at all -- it sits immediately before the "patch name" field
(0x44), the same way 0x9F sits immediately before the effect-block
section (0xA0): both are the last byte of a section, right at a boundary.

Two distinct behaviors showed up across the 5 slots, not one uniform
noise rate:
  - **25B, 47B**: read #1's value recurs in the large majority of
    reads (12/15, 13/15), with a handful of isolated garbage reads
    scattered in -- consistent with the previously-measured ~12-15%
    per-read glitch rate this whole retry design is built around.
  - **23B, 29C, 46C**: read #1's value essentially never recurs again
    (0/13, 0/14, 1/14 of the later reads match it). A different value
    (often 0x00, matching `describe_prst_offset`'s own "always 0x00 in
    every real sample seen so far" comment) dominates the remaining
    reads, but several *other* distinct garbage values also appear
    scattered among them -- not "one true value plus rare noise," but a
    genuinely unstable stream of several different values.

The garbage values themselves aren't uniformly random across 0-255 --
they cluster into two narrow bands (0x35-0x38 and 0xB5-0xB8: the same
four low values, with and without bit 7 set) rather than looking like
generic transmission-line corruption. Combined with the fact that
literally nothing outside these two boundary bytes ever disagreed in 75
read-pairs, this now looks less like "the transport occasionally mangles
a byte" and more like these two specific fields carry some small piece
of live/transient device-internal state (a counter, a flag) that isn't
meaningful stored patch content -- which would mean no amount of
retrying converges on a "true" value at these positions, because there
may not be one. This doesn't invalidate the tries=7 + delay change above
(it still helps for the 25B/47B-style genuine rare-noise case, and costs
nothing when it doesn't apply), but it does mean that change alone can't
fix the 23B/29C/46C-style case -- a real fix there would mean treating
0x43 (and reconsidering 0x9F, which was deliberately kept out of
`VERIFY_IGNORE_OFFSETS` before on the theory it might be real data) as
fields that don't need to agree at all, similar to how the tail-block
quirk is already handled. Not done here -- this needs more confidence
about what these bytes actually are before ignoring them, and the user
wanted to discuss implications before more retry-logic changes anyway.

**Then the cable experiment:** the user unplugged the guitar cable
(input) and XLR cable (output) and ran `export --all` twice -- both
perfect, 256/256, and faster. At the ~2.7%-per-slot rate the bad run
implied, two clean 256-slot runs by chance alone would be roughly 1-in-a-
million, so this looked like strong evidence that the audio I/O cables
were somehow involved (a ground loop or noise coupling disturbing
whatever's leaking into those two boundary bytes was the working theory
-- plausible since it's a mechanism that could disturb small pieces of
live device state without touching the other 1174 bytes at all).

**Then the control that undercuts that theory:** cables were plugged
back in, `export --all` run twice more -- both ALSO perfect. If cable
presence were the actual cause, reconnecting them should have brought
the failures back; it didn't. Current state after 1 bad run followed by
5 consecutive clean runs (3 cables-out, 2 cables-back-in): something
changed around the time of that first bad run, but it's no longer
possible to tell whether it was the cables specifically, a marginal
connector (audio or USB) getting reseated by the physical act of
plugging/unplugging, static discharge, or pure chance that happened to
land in that one run. **Unresolved, not reproducing.** If it recurs,
worth noting next time: how long since power-on, whether anything was
plugged/unplugged beforehand, and ideally trying `reread` on the
specific failing slot(s) again before touching anything, to see whether
the same two offsets (0x43/0x9F) are still the only things unstable.

**One more candidate ruled out:** CPU/system load (an RFI variant of the
theory -- the fan spinning up as a proxy for the computer working harder,
possibly delaying how promptly python-rtmidi's Windows backend services
its MIDI receive buffer; see that backend's own documented fixed-buffer
limitation, cited in `reread`'s docstring). Tested directly: pegged all
CPU cores at 100% (PowerShell background jobs, confirmed via Task
Manager, fan audibly engaged) and ran `export --all` twice against that
load. Both clean, 256/256. So audio cables in, audio cables out, and
now heavy CPU load, have all failed to reproduce the original failure.
Stopping active investigation here -- the single bad run remains
unexplained, but 7 consecutive clean runs across three different
conditions (cables out, cables back in, CPU pegged) is enough to call it
dormant rather than chase further today.
The next planned data point is a run on a second, different computer,
whenever that happens.

**That second-computer test happened, and found the likely real answer.**
Installing Valeton's own ASIO driver on a laptop produced much WORSE
symptoms than anything seen on the desktop -- frequent dropouts, and
`list-ports` sometimes failing to see the device at all. This is,
apparently, a known Windows/Valeton USB-MIDI incompatibility in the wider
GP-200 user community (reported to also affect firmware updates and
NAM/IR loading, i.e. not specific to this tool or even to SysEx dumps
specifically) -- with a known fix: in Device Manager, rebind the GP-200's
MIDI sub-device from Valeton's own driver to Windows' generic USB MIDI
class driver. Doing that made the laptop's connection reliable. Several
subsequent `export --all` runs there finished clean, though the new
progress dots visibly paused a couple of times per run -- `read_dump_confirmed`
needing more than one attempt to confirm a slot, exactly the scenario the
retry design exists for, and not a new problem (if anything, it's the
first time that mechanism's normal operation has been directly visible
rather than inferred from a final skip count).

This likely reframes the whole investigation above, rather than adding a
separate cause alongside it: `python-rtmidi`'s Windows backend using "a
small number of fixed-size preallocated buffers" (cited in `reread`'s own
docstring, from upstream issue reports, well before any of this session's
testing) was always the leading theory for host-side receive instability
-- this is now a concrete, real-world case of exactly that class of
problem, severe enough to be independently known and documented by the
user community, with a known driver-level cause and fix. It also offers a
tidier explanation for the earlier "always exactly file offset 0x43
and/or 0x9F, never anywhere else" finding: random transport noise would
be expected to land anywhere in a dump, but a structural bug in
fixed-size buffer reuse -- stale or leftover data bleeding into a
consistent RELATIVE position within every reconstructed message -- would
produce exactly the kind of reproducible, always-the-same-two-bytes
fingerprint observed, rather than being a second, unrelated mystery.
Whether it's Valeton's driver, Windows' own MIDI stack, or (per the
user's own account) something that changed in one direction or the other
around a Windows update either side had been depending on, this is a
driver/OS-level issue outside this tool's control, and the existing
tries=7 + graduated-delay retry design (see above) is the right and
apparently sufficient accommodation for it. No further code changes
planned on this front -- closing out this investigation thread here.

## `export`: a visible progress indicator, and skip message simplified again (2026-09-29)

With the console noise cleaned up over two earlier rounds, a long
`export --all` now prints almost nothing for the ~60-90s it takes to
read 256 slots -- direct feedback: *"the command just seems to be hung
for a few seconds... a simple progress bar. Maybe '.' printed across the
screen on the same line for every patch."* Added exactly that: one `.`
per slot (flushed immediately so it appears as it happens, not buffered
to the end), `--debug` only (in `--debug` mode the existing per-slot
`"{label}: {name!r} (...)"` line already shows progress, so the two
don't get mixed on the same line). A skipped slot still gets its own
full line -- the code prints a newline first to end the current dot-run
before the skip message, so it doesn't get swallowed into a run of dots
-- and the loop ends with one more newline so the final "Wrote N
patches..." summary doesn't land at the end of a dot-line.

Also folded in a still-outstanding wording request from a couple of
messages earlier: *"the skipped message could still be simpler.
Something like 'Error reading 47B - skipped'."* The batch skip line
dropped `describe_read_failure()`'s reason from the default message
entirely (it's `--debug`-only detail now, via the same `{detail}`
suffix export already uses elsewhere) and now reads exactly
`f"Error reading {label} - skipped{detail}"`. The single-slot export
failure message (a `sys.exit`, the only line that run produces) was
left alone -- more detail there is fine since it's not competing with
255 other lines.

Tests: `test_export_quiet_output.py` gained checks for the dot count,
the newline before the summary, no dot-noise mixed into `--debug`
output, and the new skip-message wording (replacing the now-stale check
for `describe_read_failure`'s phrase, which no longer appears by
default).
