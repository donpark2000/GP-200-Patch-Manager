# GP-200 SysEx protocol — reference

This is the settled protocol reference: what's needed to build software that
talks to the Valeton GP-200 over USB-MIDI. It contains facts and finished
design decisions only. For the investigation behind them — failed
hypotheses, test-by-test narrative, hardware A/B tests, host-driver
debugging — see `PROTOCOL_NOTES.md`, which is a running lab notebook, not a
reference.

## Licensing and provenance

Everything below is either:

- an independently reverse-engineered protocol fact (byte layouts,
  addressing, message shapes), verified against real hardware, or
- explicitly credited where it was cross-checked against, or ported from,
  someone else's published work.

Two other open-source projects target this device and are cited by name
where relevant:

- **GP200 Studio** (github.com/kabir0st/gp200-studio), GPL-3.0, a
  browser-based GP-200 editor. Several message formats below were
  originally ported from its reverse-engineering, with attribution kept in
  `gp200.py`'s own header comment as its license requires.
- **RigSheet** (github.com/ricardo-mv/rigsheet), all rights reserved. This
  project treats RigSheet as a **read-only cross-check**: RigSheet's own
  code and text are never copied here, only independently-confirmed
  protocol facts that happen to agree with what RigSheet also does. Where
  a design decision below originated from resolving a disagreement between
  GP200 Studio's assumptions and RigSheet's, that is noted, but no RigSheet
  source or text is reproduced.

Note also: GP200 Studio's own repository happens to contain a file named
`RigSheet.tsx` (a printable footswitch cheat sheet). That is unrelated to
the RigSheet project cited above — same common term, different project,
different license, different code.

## 1. Patch file format (`.prst`)

A `.prst` file is 1224 bytes, laid out as follows.

| Offset | Field | Notes |
|---|---|---|
| 0x00 | Magic `TSRP` | 4 bytes |
| 0x1C–0x1F | Per-export nonce/counter | Genuinely dynamic every export; no fixed or "correct" value to reproduce |
| 0x28–0x2D | PC-software version/checksum stamp | Only ever written by Valeton's own desktop exporter; never transmitted or expected on a device write |
| 0x2E | Slot-mirror byte | See "Slot-mirror and dead bytes" below |
| 0x34 | Slot-mirror byte | See below |
| 0x3E, 0x40 | Zeroed on official export | See "Export-only zeroed fields" below |
| 0x43 | Dead byte | Always 0x00 on the device; see "Dead bytes" below |
| 0x44 | Patch name | 16 bytes |
| 0x54 | Author | 16 bytes |
| 0x64 | Note | 40 bytes |
| 0x8C–0x9F | "Pre-effects header" gap | 0x00 in every real sample seen; only 0x9F (see below) is confirmed device-enforced by an actual write test — the other 19 bytes in this range are unconfirmed, not verified either way |
| 0x90 | Slot-mirror byte | See below |
| 0x9F | Dead byte | Always 0x00 on the device; see below |
| 0xA0 | First of 11 effect blocks | 0x48 (72) bytes each, see layout below |
| 0x460 (1120) | "Tail block", 8×12-byte entries | Device-owned, changes on every read and every save; see below |
| 0x4C6 | Checksum | 2 bytes, big-endian |

Total effect-block region: 11 × 72 = 792 bytes, spanning file offsets 0xA0
through 0x3B7 inclusive (0xA0 + 792 = 0x3B8, the first byte past the last
block).

**Effect block layout** (72 bytes, relative to the block's own start):

| Relative offset | Field |
|---|---|
| +4 | Slot index |
| +5 | Enabled flag |
| +8 | Model code / effect ID, u32 little-endian |
| +0x0C (+12) | 15 × float32 little-endian parameters (60 bytes, through the end of the block) |

The model code at `+8` is a plain numeric selector — see "Known structural
limitations" below for what this means for User-IR/NAM references.

**Checksum**: `sum(file_bytes[0:0x4C6]) & 0xFFFF`, stored big-endian at
0x4C6.

**Device dump vs. file**: a live device read returns the patch content
starting at a fixed shift from the file layout: `file_offset = dump_offset +
0x28`. A device dump does not include the file's leading ~0x28 bytes (magic,
version/firmware fields) and may end a few bytes short of the file's own
tail. Reconstructing a full, valid `.prst` file from a device dump therefore
always requires a real `.prst` "skeleton" file to supply whatever the dump
itself doesn't cover — this is normal for every read, not a special case for
template operations.

### Slot-mirror bytes (0x2E, 0x34, 0x90)

All three are populated by the device to reflect the target slot, not fixed
values — a write is expected to result in these reading back as the new
slot's own value, not the source file's. They behave differently on
**export**, however: two independent real Valeton Desktop exports of the
same untouched patch agreed that the *exporter* zeroes 0x2E specifically,
while 0x34 and 0x90 come through as the real slot value in both. This is an
export-file-only observation (from comparing files, not from testing what
the device does with 0x2E on write) — the write path continues to transmit
0x2E as ordinary content, unchanged.

### Dead bytes (0x43, 0x9F)

Confirmed by direct write test: uploading a `.prst` with deliberate garbage
at both 0x43 and 0x9F produced a write that would not verify — every retry
read back exactly 0x00 at both offsets, never the value that was sent. A
side-by-side comparison of the resulting patch against an unmodified one in
Valeton Desktop's own editor, module by module, showed no visible
difference. Conclusion: the device only ever stores 0x00 at these two
offsets regardless of input, and nothing in the editor exposes whatever they
represent. Treat both as dead/reserved fields, not patch content:

- A write should not be considered failed over a mismatch confined to these
  two offsets.
- A read does not need to converge on any particular value here; 0x00 is
  the only value worth writing back out on export, since it's the only
  value the device will ever actually persist.

These two offsets are **not** the same category as the slot-mirror bytes
above (which take a real, device-chosen, patch-specific value) — they take
one single fixed value, always. They are also more narrowly scoped than the
whole 0x8C–0xA0 gap: that wider range includes 0x90, one of the slot-mirror
bytes above, which is *not* a dead byte. Only 0x43 and 0x9F have been
confirmed dead by an actual write test; the other bytes in 0x8C–0x9F are
merely "0x00 in every sample seen so far," not confirmed device-enforced.

### Tail block (file offset 1120, "0x460")

Eight 12-byte entries; only bytes at relative `+6` and `+7` of each entry
carry anything. Device-owned, live state: it changes on every save **and**
on every plain read, with no write involved. Not patch content — exclude it
from any byte-for-byte comparison of patch content, whether verifying a
write or diffing two reads.

### Export-only zeroed fields

Comparing a large batch of this tool's own raw exports against Valeton's
official "Export All" output for the same device (245 patches in common)
found two more single bytes (0x3E, 0x40) plus three more positions within
each of the 8 tail-block entries (`+5`, `+10`, `+11`, i.e. beyond the `+6`/
`+7` already noted above) that Valeton's own exporter always writes as
0x00, while a raw device read carries a real, nonzero value there in
roughly 4–10% of patches. This is, again, an export-file-only observation
(same caveat as 0x2E above) — it says nothing about what a write does with
these bytes, only what a correct *export* should look like to match
Valeton's own output.

## 2. SysEx protocol

**Header** (every message): `F0 21 25 7E 47 50 2D 32`.

**Handshake**, required once per connection before the device will honor
writes (reads work without it):

1. Identity query (`cmd=0x11, sub=0x04`) — response is a single
   `cmd=0x12, sub=0x08` message; contents don't need parsing, only its
   arrival matters.
2. Enter-editor-mode (`cmd=0x11, sub=0x12`) — no response expected. Without
   this, writes are silently discarded; the device appears to only honor
   write traffic once told to enter editor mode.

**Reads**:

- Full-dump read: `cmd=0x11, sub=0x10`.
- Name-only read: `cmd=0x11, sub=0x20`.
- Response to either: `cmd=0x12, sub=0x18` (one or more data chunks, offset
  encoded per-chunk — see encoding below). A full dump needs multiple
  chunks; a name-only read needs one.
- Reads are self-confirming at the message level (each chunk is an explicit,
  numbered response), but the value of any individual field can still
  differ between two otherwise-successful reads of an unchanged slot — see
  "Reliability design" below for why, and what to do about it.

**Writes** (flash-chunk upload, the path used by a plain upload of a whole
patch):

- No ACK/NAK exists on the write path at all. A write is a burst of chunks
  sent at a fixed pacing interval; the only way to know whether it landed is
  a full separate read-back afterward.
- Each chunk: header + `cmd=0x12, sub=0x20` + a **fixed constant `0x09`**
  in the outer per-chunk header byte (not the target slot) + a 7-bit-encoded
  chunk offset + the nibble-encoded chunk payload + `0xF7`.
- **Addressing**: the real target slot is not the outer header byte above —
  it lives inside the nibble-decoded chunk payload's own 14-byte inner
  header, at relative offsets 6 and 12 within that header. A model that
  instead put the slot in the outer per-chunk header byte (matching what
  the outer byte looks like on a *read* response) writes that the device
  silently discards; this was the central fix that got writes to persist
  at all.
- Two fields inside that same inner-header/payload structure are
  device-recomputed and must be forced to a fixed placeholder (`0xFF`) by
  the sender rather than passed through as real content: the bytes
  corresponding to file offsets 0x34 and 0x90 (the same slot-mirror bytes
  described in the file-format section above).
- The payload content proper starts at file offset 0x2E (not 0x30); the
  two bytes this recovers (0x2E/0x2F) are ordinary content as far as the
  write path is concerned.

**Preset change** (switch the device's active slot): `cmd=0x12, sub=0x08`.

**Live parameter edits** (an alternative to whole-file upload — replay a
preset as individual live edits: toggle effect, change effect, change
parameter, then a save-commit): `cmd=0x12` with `sub=0x10` (toggle),
`sub=0x14` (effect change), and a parameter-change message carrying both a
float32 value and a separate 2-byte logarithmic "display value" field that
the device reads instead of the float for a block's first parameter. This
path is confirmed to persist to flash, but is not a complete substitute for
whole-file upload: several effect types' parameter values are known to
encode incorrectly via this path.

**Encodings**:

- *7-bit offset encoding* (multi-chunk transfers): a two-byte offset is
  `(byte1 & 0x7F) | ((byte2 & 0x7F) << 7)`.
- *Nibble encoding* (all chunk payloads): every byte becomes two SysEx-safe
  (≤0x7F) bytes, high nibble first, then low nibble.

## 3. Reliability design

A protocol-correct write (section 2) does not by itself guarantee a later
read reports back exactly what was sent, and a single read of an unchanged
slot is not guaranteed to return the same bytes as another read of the same
unchanged slot. Both are real, and both are host-receive-side effects (most
likely fixed-size buffer reuse or similar timing issues in the Windows
USB-MIDI stack — a documented, general category of flakiness, not specific
to this device), not evidence of the device's flash storage itself being
unreliable. The design below treats verification as two separate problems —
confirming what a read actually returned, and deciding which fields are
worth comparing at all — rather than one.

### Read confirmation: agree twice, or say so

A single read cannot be trusted as ground truth. The read path instead:

1. Takes up to `tries` reads (default 7).
2. Returns as soon as **any two** of them (not necessarily consecutive) are
   byte-identical, after excluding fields known not to be stable
   read-to-read (see "Fields excluded from comparison" below).
3. Raises a distinct, catchable error if none of the `tries` reads ever
   agree with each other — never silently returns an unconfirmed guess.
4. Uses a graduated delay before later attempts (a short pause before the
   4th attempt, a longer one before every attempt after that), rather than
   retrying immediately every time — cheap on the common case (most slots
   confirm within 2 reads and never see a delay at all), and gives a
   possible correlated host-side hiccup room to clear on the rare slot that
   needs more attempts.

This confirmed-read primitive is used everywhere a read result is trusted
for content: on export, before comparing a write's result, and before
backing up a slot ahead of overwriting it. Two diagnostic tools exist that
deliberately bypass this confirmation (a repeated-read command with no
writes in between, and a discrepancy inspector) — see "Diagnostic
visibility" below for why.

### Write verification: compare the confirmed re-read against the source

After a write, verification re-reads the slot (using the confirmed-read
primitive above) and compares its content against the file that was sent,
byte-for-byte, over the live-dump-covered region of the file — excluding
the fields listed below. A mismatch anywhere else is a real, actionable
write failure and should be retried.

### Fields excluded from comparison

Two different kinds of field are excluded, for two different reasons, and
they should not be merged into one "ignore list":

1. **Fields the device legitimately recomputes to a new, patch-specific
   value.** These are correctly expected to differ from the source file,
   because the whole point of a write is to change them:
   - The PC-software version/checksum stamp (0x28–0x2D) — never
     transmitted by this kind of write at all, so it can never match
     whatever the source file happened to have there.
   - The three slot-mirror bytes (0x2E, 0x34, 0x90) — expected to reflect
     the *new* target slot, not the source file's.
   - The tail block (file offset 1120, the 8×12-byte region, meaningful
     bytes at each entry's `+6`/`+7`) — live device state that changes on
     every save, and also on every plain read with no write involved at
     all, so it must be excluded from **both** write verification and
     read-to-read agreement checking.

2. **Fields the device only ever stores as one single fixed value,
   regardless of input.** This is the dead-byte pair, 0x43 and 0x9F (see
   the file-format section above). Unlike category 1, these aren't
   patch-specific — there is exactly one correct value (0x00), and the
   device already enforces it regardless of what's written. Because of
   that:
   - A write mismatch confined to these two offsets is not a real failure
     — retrying the write can never produce a different, "more correct"
     result, since the device won't store anything else there either way.
   - A read need not require two reads to *agree* on these two offsets
     either, since nothing meaningful is lost by not converging on a
     value — the correct value to write back out on export (0x00) is
     already known unconditionally.
   - Export should force both offsets to 0x00 explicitly, rather than
     trusting whatever a read happened to return, so exports stay
     deterministic.

   These two are kept as a **separate** category/constant from category 1
   in this project's own implementation, specifically so a future write
   test on some other offset doesn't get conflated with "the device
   recomputes this" — a fixed dead value and a device-recomputed
   patch-specific value call for different handling if either is ever
   revisited.

### Diagnostic visibility

Tools whose purpose is to characterize write/read reliability itself (a
repeated-read-with-no-writes probe, a write-reliability soak test) must not
use the relaxed comparison above — doing so would blind exactly the
instrument that originally found the dead-byte pair in the first place.
Those tools compare against the full, unfiltered field set (both
categories above included), so a regression or a new dead/unstable field
elsewhere would still be visible to them even after ordinary
upload/export/verify stop caring about the two known offsets.

### What retrying still cannot fix

The reliability design above narrows what's worth retrying, but does not
eliminate retries. Reads and writes both still retry for genuine reasons:
a real content mismatch anywhere outside the excluded fields is a real
failure and should be retried as before; the read-confirmation retry loop
still exists to guard against the host-side read glitch for every field
that isn't in the dead-byte/device-recomputed categories above.

## 4. Known structural limitations

### User-IR and NAM (SnapTone) references are by index, not by content

A patch's effect blocks store a plain numeric model-code selector (see the
effect-block layout above), never the underlying audio/model data itself.
User-loaded impulse responses and NAM (Neural Amp Modeler) captures are
addressed the same way as any built-in cab/amp/drive option:

- User-IR: model codes `0x0A100000`–`0x0A10001D` (30 slots), under the
  cab/IR module.
- NAM/SnapTone as an amp: `0x0F000000`–`0x0F000004` (5 slots).
- NAM/SnapTone as a distortion source: `0x0F000005`–`0x0F000009` (5 slots).

**Consequence**: exporting and re-importing a patch preserves the numeric
selector exactly, so the patch will sound correct again only if the
referenced slot still holds the same IR/NAM content — on the same device,
or on a different one. If the slot's content differs (most commonly: a
different device, or the same device after the user reloads something
different into that slot), the patch will silently select whatever is now
in that slot instead, with no way for the `.prst` format itself to detect
or flag this. This is a structural limit of the file format and the
device's own addressing scheme, not a bug in any particular tool — it
applies equally to every tool that can produce a `.prst` file. Restoring
the exact original IR/NAM file into a *different* slot number does not fix
this either, since the reference is to a slot number, not a filename or
content identity.

Moving the actual IR/NAM binary content itself (as opposed to detecting
and reporting the dependency) is not currently a solved problem by any
known reference implementation — no project surveyed queries or transfers
that content on export, only the numeric selector and (separately, for
User-IR only) the slot's display name.

### Patch names are 16 raw bytes; character set beyond ASCII is unconfirmed

The name field (0x44, 16 bytes) is transmitted and stored as raw bytes with
no assumptions imposed by this reference — round-tripping it through a
file is always safe regardless of what it contains. What is **not**
confirmed is which character set(s) the device's own hardware input (or
any companion app) can actually produce for a name outside plain ASCII —
whether that's simply unsupported by the hardware's own input method, a
vendor-specific encoding on non-English-market firmware, UTF-8, or
something else. Software built against this reference should treat the
name field as opaque bytes for storage/transport purposes, and should not
assume ASCII (or any other specific encoding) when *decoding* it for
display — decode defensively (e.g. flagging or substituting undecodable
bytes) rather than producing a confident-but-wrong display string.

## Quick reference: byte offset summary

| Offset(s) | Category | Rule |
|---|---|---|
| 0x1C–0x1F | Per-export nonce | No fixed value; don't try to reproduce it |
| 0x28–0x2D | Version/checksum stamp | Exporter-only; never sent on write |
| 0x2E | Slot-mirror (export-zeroed) | Device-recomputed on write; zeroed by official export |
| 0x34, 0x90 | Slot-mirror | Device-recomputed on write; carries real slot value on export |
| 0x3E, 0x40 | Export-only zeroed | Real value on raw read; official export always shows 0x00 |
| 0x43 | Dead byte | Always 0x00 on the device; exclude from write/read comparison; force to 0x00 on export |
| 0x9F | Dead byte | Same as 0x43 |
| 1120 + q·12 + {5,6,7,10,11} for q in 0..7 | Tail block / export-zeroed | Live device state; changes on every read and save; exclude from all comparison; zero on export |
| 0x4C6 | Checksum | Recompute after any content change |
