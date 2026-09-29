#!/usr/bin/env python3
"""
gp200.py - command-line patch manager for the Valeton GP-200.

Talks live SysEx over USB-MIDI to read, write, and clear device slots.
Does not know about effects, amps or cabs; it only moves whole .prst
patches between the pedal and files on disk.

PROTOCOL CREDIT: the SysEx message formats below (buildReadRequest,
buildUploadImage/Chunks, buildPresetChange, the nibble encoding, and the
decoded-dump-mirrors-the-file-shifted-by-0x28 relationship) are ported from
Kabir S. Tamari's GP200 Studio (github.com/kabir0st/gp200-studio), GPL-3.0,
which reverse-engineered them from USB captures of Valeton's own editor.
This script is for personal use against your own pedal.

STATUS: read/export path is tested and trustworthy against real hardware.
The write path has gone through several rounds of real-hardware testing;
its flash-chunk upload (build_upload_image/build_upload_chunks, used by
plain `upload`/`apply-template`) was cross-checked against a
SECOND independent reverse-engineering of this protocol (the RigSheet web
app, github.com/ricardo-mv/rigsheet) after GP200-Studio-derived writes were
silently discarded by the device. That check found the real target slot is
supposed to live inside the nibble-encoded payload itself (at two fixed
offsets), not in the outer per-chunk SysEx header the way GP200 Studio's
own capture assumed -- the outer header's byte there is a fixed constant on
every write, the same way it already is on every read. This script now
follows the RigSheet model; it has not yet been confirmed against real
hardware (see build_upload_image's docstring for the exact fix). Because
that also means the outer header's "slot byte" was never the addressing
mechanism, --diag-write's high-slot/bank-33+ aliasing premise is now in
question too -- treat its results with that in mind until re-verified.
A separate write path, --method live (replaying the preset as individual
live-parameter edits instead of a whole-file chunk upload), IS confirmed to
persist real changes to flash, but has a known bug: individual effect
parameter values come out wrong for several effect types (see
encode_display_value's docstring).
  - Exactly how far a device read's payload extends (see EXPORT NOTES
    below); anything beyond that point is left as the skeleton file's own
    value rather than the pedal's real current value.

EXPORT NOTES:
  A device read returns the patch as a positional mirror of the .prst file
  content, offset by a fixed 0x28 (40) bytes: file_offset = dump_offset +
  0x28. Export takes a real .prst file as a "skeleton" (--skeleton, or the
  bundled default), overlays the live dump onto that byte range, and
  recomputes the checksum. Anything in the file before 0x28 (the magic,
  device and firmware fields) or beyond wherever the dump actually ends is
  left as the skeleton's own value, since a device read may not carry it.

Install once:
    pip install mido python-rtmidi

    Python version note: python-rtmidi is a compiled C extension, and its
    prebuilt wheels lag behind the newest CPython releases. If plain `pip
    install python-rtmidi` fails, or seems to install but this script still
    can't be run with plain `python`, that's almost always because the
    default `python` on your system is newer than what python-rtmidi
    currently ships a wheel for -- not a problem with this script. This
    project has been developed and tested throughout against Python 3.12
    specifically. On Windows, install Python 3.12 and target it explicitly
    with the `py` launcher instead of plain `python`/`pip`:
        py -3.12 -m pip install mido python-rtmidi
        py -3.12 gp200.py list-ports
    (macOS/Linux: typically `python3.12` in place of `py -3.12`.)

Examples (Windows; use `python3.12`/`python3`/`python` in place of
`py -3.12` if that's what actually works on your system -- see the version
note above):
    py -3.12 gp200.py list-ports
    py -3.12 gp200.py list                                  # names of all 256 slots
    py -3.12 gp200.py read 34-B                              # just the name, fast
    py -3.12 gp200.py export 34-B -o backup.prst
    py -3.12 gp200.py export --start 34-A --end 36-D -o a_few_banks.zip
    py -3.12 gp200.py export --all -o all_slots.zip
    py -3.12 gp200.py upload backup.prst 34-B                # one file, one slot
    py -3.12 gp200.py upload a.prst b.prst c.prst 10-A        # several files, consecutive slots from 10-A
    py -3.12 gp200.py upload backup.zip 10-A                  # a zip of files, same as above
    py -3.12 gp200.py apply-template blank.prst 30-A 34-D
    py -3.12 gp200.py diag-write 34-B --template blank.prst  # the high-slot test
    py -3.12 gp200.py raw-sweep --all                         # single-read vs. confirmed, all slots
"""
import argparse
import atexit
import base64
import math
import platform
import re
import struct
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

try:
    import mido
except ImportError:
    sys.exit(
        "This script needs mido and python-rtmidi.\n"
        "Install with:  pip install mido python-rtmidi\n"
        "\n"
        "If that install itself failed (not just this import), it's almost\n"
        "always because python-rtmidi is a compiled extension and your\n"
        "current Python is newer than what it has a prebuilt wheel for yet --\n"
        "not a problem with this script. This project has been developed and\n"
        "tested against Python 3.12 specifically. On Windows, install Python\n"
        "3.12 and target it explicitly with the `py` launcher instead of\n"
        "plain `python`/`pip`:\n"
        "    py -3.12 -m pip install mido python-rtmidi\n"
        "    py -3.12 gp200.py ...\n"
        "(macOS/Linux: typically `python3.12` in place of `py -3.12`.)")

# Bumped whenever a change ships, purely so a --log-file capture (or a bug
# report) can be tied to an exact revision without guessing from behavior.
SCRIPT_VERSION = "2026-09-27"

HEADER = bytes([0xF0, 0x21, 0x25, 0x7E, 0x47, 0x50, 0x2D, 0x32])
TOTAL_SLOTS = 256
DUMP_TO_FILE_SHIFT = 0x28          # file_offset = dump_offset + 0x28
DEFAULT_SKELETON = Path(__file__).with_name("skeleton.prst")

# A byte-for-byte copy of the bundled skeleton.prst, embedded so a missing or
# accidentally-deleted skeleton.prst next to this script can never block an
# export -- see resolve_skeleton_bytes() and EXPORT NOTES above. The bytes
# this file supplies are model/firmware-constant scaffolding (magic, version,
# a couple of trailing bytes), never patch content, so any valid .prst is
# equally usable here; this is just a convenient one to fall back on.
_EMBEDDED_SKELETON_B64 = (
    "VFNSUAAAAAAAAAAGAAAAADItUEcAAQEAAAAAAJjuPQAoAAAAlAQAAE1SQVCUBAAAAgBYAJIAeAAy"
    "AAAAAAAAAAAAAABJdCdzIEdQLTIwMAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAIABAAkgAEBAABAgMEBQYHCAkKABQARAAAAA8AAAAA"
    "AAAAoEEAAEhCAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAABQARAABAA8AAQAABQAASEIAAEhCAABIQgAASEIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAABQARAACAA8AAAAAAwAAIEIAAIxCAABIQgAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABQARAADAA8AAQAABwAA8EEAAEhCAABI"
    "QgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABQARAAEAA8A"
    "GwAAAAAAoEEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAABQARAAFAA8AAAAACgAAgD8AAEhCAADwQQAAcEEAAHBCAACYQQBCnEYAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAABQARAAGAA8ANQAAAQAAAAAAAAAAAAAAAAAAAAAAAAAAAABI"
    "QgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABQARAAHAA8AAQAABAAASEIAAAA/"
    "AABIQgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABQARAAI"
    "AA8AHQAACwAAoEEAAPpDAACgQTMzd0IAAMhCAABIQgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAABQARAAJAA8AAAAADAAA8EEAAEhCAABIQgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABQARAAKAQ8AAwAABgAAyEIAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAwADAAACgAAAADIQgAAAAAM"
    "AAwAAf8AAAAAyEIAAAAADAAMAAL/AAAAAMhCAAAAAAwADAAQAQMAAADIQgAAAAAMAAwAEf8AAAAA"
    "yEIAAAAADAAMABL/AAAAAMhCAAAAAAwADAAg/wAAAADIQgAAAAAMAAwAIf8AAAAAyEIAAAAADAAM"
    "ACL/AAAAAMhCAAAAABAABAAA/wAAEAAEAAH/AAAQAAQAAgsAAA8ACAAAAAAAAAAAAA8ACAABAAAA"
    "AAAAAA8ACAACAAAAAAAAAA8ACAADAAAAAAAAAA8ACAAEAAAAAAAAAA8ACAAFAAAAAAAAAA8ACAAG"
    "AAAAAAAAAA8ACAAHAAAAAAAAAMAEAAAAAEAO"
)
READ_TIMEOUT_S = 2.0
RETRY_COUNT = 1

# ---------------------------------------------------------------- slots ----

def slot_to_label(slot: int) -> str:
    if not 0 <= slot < TOTAL_SLOTS:
        raise ValueError(f"slot {slot} out of range 0-{TOTAL_SLOTS - 1}")
    bank = slot // 4 + 1
    letter = "ABCD"[slot % 4]
    return f"{bank}{letter}"


def label_to_slot(label: str) -> int:
    label = label.strip().upper().replace("-", "")
    if len(label) < 2 or label[-1] not in "ABCD" or not label[:-1].isdigit():
        raise ValueError(f"invalid slot label {label!r}; expected e.g. 34B or 34-B")
    bank = int(label[:-1])
    letter = "ABCD".index(label[-1])
    slot = (bank - 1) * 4 + letter
    if not 0 <= slot < TOTAL_SLOTS:
        raise ValueError(f"slot label {label!r} is out of range (banks 1-64, A-D)")
    return slot


def parse_slot_range(start_label: str, end_label: str):
    """Inclusive range of slot indices, in natural pedal order."""
    a, b = label_to_slot(start_label), label_to_slot(end_label)
    if a > b:
        raise ValueError(f"{start_label} comes after {end_label}")
    return list(range(a, b + 1))

# -------------------------------------------------------------- nibbles ----

def nibble_encode(data: bytes) -> bytes:
    out = bytearray(len(data) * 2)
    for i, b in enumerate(data):
        out[2 * i] = (b >> 4) & 0x0F
        out[2 * i + 1] = b & 0x0F
    return bytes(out)


def nibble_decode(data: bytes) -> bytes:
    out = bytearray(len(data) // 2)
    for i in range(len(out)):
        out[i] = ((data[2 * i] & 0x0F) << 4) | (data[2 * i + 1] & 0x0F)
    return bytes(out)

# ------------------------------------------------------- message builders ---

def build_read_request(slot: int, name_only: bool = False) -> bytes:
    sh, sl = (slot >> 4) & 0x0F, slot & 0x0F
    msg = bytearray([
        *HEADER,
        0x11, 0x20 if name_only else 0x10,
        0, 0, 0, 0, 0, 0, 0, 0,
        0x04, 0, 0, 0,
        0x01, 0,
        0,
        sh, sl,
        0, 0, 0,
        0x01, 0,
        0, 0,
        0x04, 0, 0,
        sh, sl,
        0, 0,
        sh, sl,
        0, 0,
        0xF7,
    ])
    return bytes(msg)


def build_preset_change(slot: int) -> bytes:
    sh, sl = (slot >> 4) & 0x0F, slot & 0x0F
    return bytes([
        *HEADER,
        0x12, 0x08,
        0, 0, 0, 0,
        0x08, 0x01,
        0, 0,
        0x04, 0, 0, 0,
        0, 0, 0,
        sh, sl,
        0, 0,
        0xF7,
    ])


def build_identity_query() -> bytes:
    """Sent once at connect time by the reference editor, before it will
    accept editor/write traffic. Response is a single cmd=0x12 sub=0x08
    message; we don't parse its contents, just wait for it."""
    return bytes([*HEADER, 0x11, 0x04, 0,0,0,0, 0x01,0x02, 0,0,0,0,0, 0xF7])


def build_enter_editor_mode() -> bytes:
    """Sent once at connect time, right after the identity query. No response
    is expected. Reads work fine without this, but flash-write chunks were
    being silently discarded by the device until this was added -- the
    GP-200 appears to only honor writes once it's been told to enter editor
    mode, the same way Valeton's own desktop app and GP200 Studio do."""
    return bytes([*HEADER, 0x11, 0x12, 0,0,0, 0xF7])


def build_upload_image(file_bytes: bytes, slot: int) -> bytes:
    """Builds the raw (pre-nibble-encoded) payload for a flash-chunk upload.

    Revised after cross-checking against a second, independent reverse-
    engineering of this protocol (the RigSheet web app's `msgGuardar`), which
    disagrees with the GP200-Studio-derived version of this function on
    exactly the two points that would explain why our writes were being
    silently discarded while the official editor's writes worked:

    1. The 14-byte inner header carries the REAL target slot number at
       relative offsets 6 and 12 -- not 0xFF. GP200 Studio's own capture
       blanks both to 0xFF, which (if this model is right) tells the device
       "no slot", and the device drops the write rather than guess.
    2. The file content appended after that header starts two bytes earlier
       than previously assumed: at 0x2E, not 0x30. The two bytes this
       recovers (0x2E/0x2F) are ordinary content as far as the write/verify
       logic is concerned -- they are not one of the fields the device
       recomputes itself.

    The two fields the device DOES recompute itself (the slot-mirror bytes
    at file offsets 0x34 and 0x90) are still blanked to 0xFF here, same as
    before -- that part of the original port was already correct.

    OPEN QUESTION (2026-09-27, not yet re-tested): a later, unrelated
    finding (see EXPORT_ZEROED_SLOT_ECHO_OFFSET) shows byte 0x2E is
    consistently 0x00 in Valeton's own official exports, while 0x34/0x90
    aren't -- which sits oddly next to point 2 above calling 0x2E "ordinary
    content, not recomputed." That finding came from comparing EXPORTED
    FILES, not from testing what this function's write path actually does
    with 0x2E on real hardware, so it doesn't by itself justify blanking
    0x2E to 0xFF here too -- this function's behavior is unchanged pending
    an actual write-side test (e.g. write two files identical except at
    0x2E, read back, see whether the device echoes our value or its own).
    """
    FOOTER_LEN = 8
    CONTENT_START = 0x2E
    content = file_bytes[CONTENT_START: len(file_bytes) - FOOTER_LEN]
    HEADER_LEN = 14
    image = bytearray(HEADER_LEN + len(content))
    image[0:HEADER_LEN] = bytes([
        0x00, 0x00, 0x04, 0x00, 0x01, 0x00, slot & 0xFF, 0x00,
        0x01, 0x00, 0x04, 0x00, slot & 0xFF, 0x00,
    ])
    image[HEADER_LEN:] = content
    image[20] = 0xFF   # file offset 0x34 -- device-owned slot-mirror byte
    image[112] = 0xFF  # file offset 0x90 -- device-owned slot-mirror byte
    return bytes(image)


def build_upload_chunks(image: bytes, slot: int):
    """NOTE: the outer per-chunk header byte right after cmd/sub used to be
    `slot & 0x7F` here, matching GP200 Studio's capture. RigSheet's
    independent implementation instead uses a fixed constant (0x09) there on
    every write regardless of target slot -- the same constant it (and we)
    already expect unconditionally on every *read* response. The real slot
    now lives only inside the nibble-decoded payload (see build_upload_image).
    `slot` is kept as a parameter for logging/symmetry but no longer masked
    into this byte; the old 7-bit-masking concern this file's --diag-write
    command was built to test may no longer apply under this model."""
    CHUNK_RAW = 183
    chunks = []
    for off in range(0, len(image), CHUNK_RAW):
        raw = image[off: off + CHUNK_RAW]
        nibble = nibble_encode(raw)
        msg = bytearray(13 + len(nibble) + 1)
        msg[0:10] = bytes([*HEADER, 0x12, 0x20])
        msg[10] = 0x09
        msg[11] = off & 0x7F
        msg[12] = (off >> 7) & 0x7F
        msg[13:13 + len(nibble)] = nibble
        msg[-1] = 0xF7
        chunks.append(bytes(msg))
    return chunks

# ------------------------------------------------ live-edit write builders --
# The GP200 Studio project's OWN history (see its git log) treats the whole-
# file flash-chunk upload above as never fully reliable: it was gated behind
# a dev-only flag on day one with the note "the writePresetToSlot flow
# (toggle + params + save-commit) is the supported path", was briefly
# unblocked, and within hours of that same day picked up permanent, unresolved
# warnings about the device silently discarding staged writes. This section
# ports that OTHER path instead: replay a preset as a sequence of live
# parameter edits (the same messages a person turning knobs on the device
# would generate), then a save-commit to persist it -- the one write path the
# project has trusted since its very first commit.

def encode_display_value(value: float) -> bytes:
    """The 2-byte logarithmic 'display value' field a Param Change message
    carries alongside the float32 param -- the device reads THIS field (not
    the float) for a block's first parameter."""
    u16 = 0
    if value > 0:
        u16 = round(16367 + 16 * math.log2(value))
        u16 = max(0, min(0xFFFF, u16))
    return bytes([u16 & 0xFF, (u16 >> 8) & 0xFF])


def build_toggle_effect(block_index: int, enabled: bool) -> bytes:
    return bytes([
        *HEADER, 0x12, 0x10,
        0,0,0,0,0,0,0,0,
        0x04,0,0,0,
        0,0,0,
        0,0,
        0,0,
        0x01,0x05,
        0,0,0,
        0x04,0,0,0,
        block_index & 0x0F,
        0,
        0x01 if enabled else 0x00,
        0x09,0x0C,
        0x00,0x02,
        0xF7,
    ])


def build_effect_change(block_index: int, effect_id: int) -> bytes:
    module_type = (effect_id >> 24) & 0xFF
    variant = effect_id & 0xFF
    return bytes([
        *HEADER, 0x12, 0x14,
        0,0,0,0,0,0,0,0,
        0x04,0,0,0,
        0,0,0,0,0,0,0,
        0x01,0x06,
        0,0,0,
        0x08,
        0,0,0,
        block_index & 0x0F,
        0,0,
        0x07,0x06,0x00,0x02,
        (variant >> 4) & 0x0F,
        variant & 0x0F,
        0,0,0,0,0,
        module_type & 0xFF,
        0xF7,
    ])


def build_param_change(block_index: int, param_index: int, effect_id: int, value: float) -> bytes:
    decoded = bytearray(24)
    decoded[2] = 0x04
    decoded[8] = 0x05
    decoded[10] = 0x0C
    decoded[12] = block_index
    decoded[13] = param_index
    decoded[14:16] = encode_display_value(value)
    struct.pack_into("<I", decoded, 16, effect_id & 0xFFFFFFFF)
    struct.pack_into("<f", decoded, 20, value)
    nibbles = nibble_encode(bytes(decoded))
    msg = bytearray(62)
    msg[0:13] = bytes([*HEADER, 0x12, 0x18, 0, 0, 0])
    msg[13:13 + len(nibbles)] = nibbles
    msg[61] = 0xF7
    return bytes(msg)


def build_patch_setting(target: int, value: int) -> bytes:
    msg = bytearray([
        *HEADER, 0x12, 0x10,
        0,0,0,0,0,0,0,0,
        0x04,0,0,0,
        0,0,0,
        0,0,
        0,0,
        0x00,0x06,
        0,0,0,
        0x04,0,0,0,
        target & 0x0F,
        0,
        0,
        (value >> 4) & 0x0F,
        value & 0x0F,
        0,0,
        0xF7,
    ])
    if target == 0x06 and value > 127:          # PAN left-of-center
        msg[43] = 0x0F
        msg[44] = 0x0F
    if target == 0x01 and value > 255:          # tempo needing the extra nibble
        msg[41] = (value >> 4) & 0x0F
        msg[42] = value & 0x0F
        msg[43] = (value >> 12) & 0x0F
        msg[44] = (value >> 8) & 0x0F
    return bytes(msg)


def build_reorder_effects(order, send: int, ret: int) -> bytes:
    decoded = bytearray(32)
    decoded[2] = 0x04
    decoded[8] = 0x08
    decoded[10] = 0x10
    decoded[14] = send & 0xFF
    decoded[15] = ret & 0xFF
    for i, slot_index in enumerate(order[:11]):
        decoded[16 + i] = slot_index
    decoded[27] = 0x44
    nibbles = nibble_encode(bytes(decoded))
    msg = bytearray(78)
    msg[0:13] = bytes([*HEADER, 0x12, 0x20, 0, 0, 0])
    msg[13:13 + len(nibbles)] = nibbles
    msg[77] = 0xF7
    return bytes(msg)


def build_save_commit(preset_name: str, slot: int) -> bytes:
    """Persists the device's current live-edit buffer to flash. decoded[4]
    carries the full absolute slot number per the reference implementation
    (its own comments disagree with each other on this point -- one says
    full slot, one says sub-slot letter index -- so this is a place to
    watch closely if a save-commit lands in the wrong slot)."""
    decoded = bytearray(24)
    decoded[0] = 0x03
    decoded[1] = 0x20
    decoded[2] = 0x14
    decoded[4] = slot & 0xFF
    name_bytes = preset_name.encode("ascii", "replace")[:16]
    decoded[8:8 + len(name_bytes)] = name_bytes
    nibbles = nibble_encode(bytes(decoded))
    msg = bytearray(62)
    msg[0:13] = bytes([*HEADER, 0x12, 0x18, 0, 0, 0])
    msg[13:13 + len(nibbles)] = nibbles
    msg[61] = 0xF7
    return bytes(msg)


def build_author_name(author: str) -> bytes:
    decoded = bytearray(32)
    decoded[2] = 0x04
    decoded[6] = 0x01
    decoded[8] = 0x09
    decoded[10] = 0x14
    decoded[12] = 0x01
    decoded[14] = 0x70
    decoded[15] = 0x0B
    name_bytes = author.encode("ascii", "replace")[:16]
    decoded[16:16 + len(name_bytes)] = name_bytes
    nibbles = nibble_encode(bytes(decoded))
    msg = bytearray(78)
    msg[0:13] = bytes([*HEADER, 0x12, 0x20, 0, 0, 0])
    msg[13:13 + len(nibbles)] = nibbles
    msg[77] = 0xF7
    return bytes(msg)

# ------------------------------------------------------------- parsing -----

def is_sysex(data: bytes, cmd: int, sub: int) -> bool:
    return (len(data) > 10 and data[0:8] == HEADER and
            data[8] == cmd and data[9] == sub)


def extract_name_field(decoded: bytes) -> str:
    """Pull the 16-byte name field out of an already nibble-decoded dump
    (offset 28:44) and turn it into a display string.

    Decodes as ASCII with '?' substitution for anything outside that range,
    rather than mapping each byte 1:1 onto a Unicode code point (Latin-1):
    the latter produces confident-looking mojibake for any non-ASCII byte
    (e.g. a Chinese-firmware patch name) instead of an honest "?", and the
    device's own supported character set for names is not independently
    confirmed -- see PROTOCOL_NOTES.md. This only affects what's printed to
    the console and the filename export derives; the actual name bytes
    written into an exported .prst file come from the raw dump overlay in
    build_prst_from_dump(), untouched by this function either way."""
    name = bytearray()
    for b in decoded[28:44]:
        if b == 0:
            break
        name.append(b)
    return name.decode("ascii", "replace")


def parse_preset_name(sysex_msg: bytes) -> str:
    nibble_data = sysex_msg[13:-1]
    decoded = nibble_decode(nibble_data)
    return extract_name_field(decoded)


# ---- User-IR / SnapTone (NAM) slot-reference detection (PROTOCOL_NOTES.md,
# Finding 11, 2026-09-28/29). A patch's 11 effect blocks each store a plain
# 4-byte model code picking which cab/amp/drive that position uses -- and a
# sub-range of those codes doesn't mean a specific built-in sound at all,
# it means "whatever the user loaded into User-IR/SnapTone slot N". Offsets
# and code ranges below are read (not copied) from two already-credited
# reference projects, not from any new hardware capture:
#   - Effect block layout in our own nibble-decoded `decoded` representation:
#     GP200 Studio's PRSTDecoder confirms file offset 0xA0 (11 x 72-byte
#     blocks, 4-byte LE model code at block offset 8) against real .prst
#     files; its SysExCodec.parsePresetFromDecoded confirms the SAME blocks
#     sit at 120 (0xA0 - 0x28) in the raw-dump layout this tool's `decoded`
#     already uses elsewhere (extract_name_field's offset 28 is that same
#     -0x28 shift applied to the file's name offset 0x44).
#   - User-IR code range (CAB module, codes 0x0A100000+, 30 entries, all
#     named generically "User IR"): GP200 Studio's own effect-code table,
#     cross-checked against a comment in its connect sequence noting the
#     device exposes "30 User-IR slot names" via its own SysEx query.
#   - SnapTone/NAM code range: GP200 Studio's table has no entries for
#     these; RigSheet's independently reverse-engineered table does --
#     5 codes tagged module AMP (0x0F000000-0x0F000004) and 5 tagged
#     module DST (0x0F000005-0x0F000009), every one named "SnapTone" with
#     the description "For importing and using the .nam file". Both ranges
#     address the same 5 physical capture slots (0-4), just from either
#     chain position, hence the shared local numbering below.
# DUMP_-prefixed to keep these distinct from the file-offset-based
# EFFECT_BLOCK_START/EFFECT_BLOCK_SIZE locals used elsewhere in this file
# (prst_to_preset and the raw-dump-diff offset namer both use 0xA0, the
# .prst FILE layout's block start -- a different number from this one even
# though the block/field sizes are identical). Same name, different meaning,
# so DUMP_ makes clear these are decoded-dump-offset-based (0x78 = 0xA0 - 0x28).
DUMP_EFFECT_BLOCK_COUNT = 11
DUMP_EFFECT_BLOCK_START = 0x78   # 120: file offset 0xA0, shifted -0x28 into `decoded`
DUMP_EFFECT_BLOCK_SIZE = 0x48    # 72 bytes per block, same in both layouts
DUMP_EFFECT_MODEL_OFFSET = 8     # LE uint32 model code, within a block

USER_IR_BASE = 0x0A100000
USER_IR_COUNT = 30
SNAPTONE_AMP_BASE = 0x0F000000
SNAPTONE_DST_BASE = 0x0F000005
SNAPTONE_COUNT = 5


def describe_ir_nam_dependency(effect_id: int) -> str | None:
    """Human-readable note if `effect_id` (a decoded effect block's model
    code) means "whatever is loaded into User-IR/SnapTone slot N" rather
    than a specific built-in sound. Returns None for an ordinary built-in
    effect code, which is the vast majority of them.

    The SnapTone AMP-position and DST-position code ranges both address the
    same 5 physical NAM-capture slots (see the module comment above), but
    they are NOT interchangeable in a patch: a block using the AMP-position
    code is using that capture as the amp, a block using the DST-position
    code is using the SAME capture as the drive/distortion -- two distinct,
    independently-loadable uses of the same slot. Collapsing both ranges
    into one description would hide that distinction, so each is tagged
    with which position it was found in (2026-09-29, per direct request:
    "there are snaptone slots in both the amp module and the dist module...
    our warning should clarify which")."""
    if USER_IR_BASE <= effect_id < USER_IR_BASE + USER_IR_COUNT:
        return f"User-IR slot {effect_id - USER_IR_BASE}"
    if SNAPTONE_AMP_BASE <= effect_id < SNAPTONE_AMP_BASE + SNAPTONE_COUNT:
        return f"SnapTone (NAM) slot {effect_id - SNAPTONE_AMP_BASE} (amp)"
    if SNAPTONE_DST_BASE <= effect_id < SNAPTONE_DST_BASE + SNAPTONE_COUNT:
        return f"SnapTone (NAM) slot {effect_id - SNAPTONE_DST_BASE} (dist)"
    return None


def find_ir_nam_dependencies(decoded: bytes) -> list[str]:
    """Scan a decoded dump's ALL 11 effect blocks for any User-IR/SnapTone
    references (see describe_ir_nam_dependency), not just the first one
    found -- a single patch can perfectly well reference a User-IR AND both
    a SnapTone-as-amp AND a SnapTone-as-dist at once (2026-09-29: confirmed
    this needs to be an explicit guarantee, not just an accident of the
    implementation, per direct request: "it is perfectly possible that one
    patch uses both as well as an IR. So the warning should ensure all are
    mentioned"). Returns a list of descriptions in chain-block order, empty
    if the patch uses none -- which is the common case, so callers should
    treat an empty list as "nothing to report" rather than an error.
    Defensive about a short or malformed dump: a block that doesn't fully
    fit is silently skipped rather than raising, since this is an
    informational note, not something export/upload's correctness depends
    on."""
    found = []
    for i in range(DUMP_EFFECT_BLOCK_COUNT):
        base = DUMP_EFFECT_BLOCK_START + i * DUMP_EFFECT_BLOCK_SIZE
        end = base + DUMP_EFFECT_MODEL_OFFSET + 4
        if end > len(decoded):
            continue
        effect_id = int.from_bytes(decoded[base + DUMP_EFFECT_MODEL_OFFSET:end], "little")
        desc = describe_ir_nam_dependency(effect_id)
        if desc:
            found.append(desc)
    return found


def assemble_chunks(chunks) -> bytes:
    def offset_of(msg):
        # Offsets are split 7 bits per SysEx data byte (each byte must be
        # 0x00-0x7F), matching the encoding build_upload_chunks() already
        # uses on the write side: low 7 bits in msg[11], next 7 bits in
        # msg[12]. A naive 8-bit pair (msg[11] | (msg[12] << 8)) happens to
        # stay monotonic in send order for a single read, so it can look
        # like it works, but it computes the wrong byte offsets and will
        # misorder/misplace chunks in general (e.g. export --all).
        return (msg[11] & 0x7F) | ((msg[12] & 0x7F) << 7)
    ordered = sorted(chunks, key=offset_of)
    nibble_parts = b"".join(msg[13:-1] for msg in ordered)
    return nibble_decode(nibble_parts)

# --------------------------------------------------------------- .prst -----

PRST_LEN = 1224
CHECKSUM_OFF = 0x4C6
CONTENT_FILE_START = DUMP_TO_FILE_SHIFT  # 0x28


def _validate_skeleton(data: bytes, source: str) -> bytearray:
    data = bytearray(data)
    if len(data) != PRST_LEN or data[0:4] != b"TSRP":
        raise ValueError(f"{source} is not a valid 1224-byte .prst skeleton")
    return data


def load_skeleton(path: Path) -> bytearray:
    return _validate_skeleton(path.read_bytes(), str(path))


def resolve_skeleton_bytes(explicit_path) -> bytearray:
    """Picks what to reconstruct a full .prst file from a live device dump
    (see EXPORT NOTES above). Order: an explicit --skeleton path if given;
    else skeleton.prst next to this script, if it's there; else the copy
    embedded directly in this file. Falling back to the embedded copy is
    always safe -- the borrowed bytes are model/firmware-constant, not
    patch-specific, so it makes no difference which valid .prst supplies
    them."""
    if explicit_path:
        return load_skeleton(Path(explicit_path))
    if DEFAULT_SKELETON.exists():
        return load_skeleton(DEFAULT_SKELETON)
    return _validate_skeleton(base64.b64decode(_EMBEDDED_SKELETON_B64), "<embedded skeleton>")


def prst_checksum(data: bytes) -> int:
    return sum(data[:CHECKSUM_OFF]) & 0xFFFF


def prst_file_name(file_bytes: bytes) -> str:
    """The patch name stored in a .prst file on disk (offset 0x44, 16 bytes,
    NUL-terminated ASCII) -- used to sanity-check a write against what the
    device reads back afterward."""
    raw = file_bytes[0x44:0x54]
    end = raw.find(b"\x00")
    if end != -1:
        raw = raw[:end]
    return raw.decode("ascii", "replace")


def safe_filename(name: str) -> str:
    """Sanitize a patch name for use as a filename."""
    name = name.strip() or "patch"
    name = re.sub(r'[\\/:*?"<>|]+', "_", name)
    return name.strip(" .") or "patch"


def decode_prst(file_bytes: bytes) -> dict:
    """Parse a .prst file into its structured fields, ported from GP200
    Studio's PRSTDecoder. Used by write_slot_live to replay a file as a
    sequence of live parameter edits rather than a raw flash-chunk blob."""
    if len(file_bytes) not in (1224, 1176):
        raise ValueError(f"invalid .prst file: expected 1224 or 1176 bytes, got {len(file_bytes)}")
    if file_bytes[0:4] != b"TSRP":
        raise ValueError("invalid .prst file: magic header not found")

    def read_ascii(off, n):
        raw = file_bytes[off:off + n]
        end = raw.find(b"\x00")
        if end != -1:
            raw = raw[:end]
        return raw.decode("ascii", "replace")

    patch_name = read_ascii(0x44, 16).strip()
    author = read_ascii(0x54, 16)

    EFFECT_BLOCK_START = 0xA0
    EFFECT_BLOCK_SIZE = 0x48
    by_block = []
    for i in range(11):
        base = EFFECT_BLOCK_START + i * EFFECT_BLOCK_SIZE
        slot_index = file_bytes[base + 4]
        enabled = file_bytes[base + 5] == 1
        (effect_id,) = struct.unpack_from("<I", file_bytes, base + 8)
        params = []
        for p in range(15):
            (raw,) = struct.unpack_from("<f", file_bytes, base + 0x0C + p * 4)
            params.append(raw if math.isfinite(raw) else 0.0)
        by_block.append({"slotIndex": slot_index, "enabled": enabled,
                          "effectId": effect_id, "params": params})

    # Reorder by playback order (routing bytes at 0x94-0x9E), defensively:
    # keep valid in-range non-duplicate bytes in file order, then append
    # whatever slots were left out, so the result is always a full 0..10
    # permutation even if the stored routing is partially corrupt.
    routing = []
    seen = set()
    for i in range(11):
        v = file_bytes[0x94 + i]
        if v < 11 and v not in seen:
            routing.append(v)
            seen.add(v)
    for si in range(11):
        if si not in seen:
            routing.append(si)
    effects = [by_block[si] for si in routing]

    raw_send = file_bytes[0x92]
    raw_return = file_bytes[0x93]
    fx_loop_send = raw_send if 1 <= raw_send <= 11 else 4
    fx_loop_return = raw_return if 1 <= raw_return <= 11 else 4

    raw_vol = file_bytes[0x38]
    patch_volume = raw_vol if raw_vol <= 100 else 50
    (patch_tempo,) = struct.unpack_from("<H", file_bytes, 0x36)
    (raw_pan,) = struct.unpack_from("<H", file_bytes, 0x3A)
    pan_signed = raw_pan - 0x10000 if raw_pan > 0x7FFF else raw_pan
    patch_pan = pan_signed if -50 <= pan_signed <= 50 else 0

    return {
        "patchName": patch_name,
        "author": author or None,
        "effects": effects,
        "fxLoopSend": fx_loop_send,
        "fxLoopReturn": fx_loop_return,
        "patchVolume": patch_volume,
        "patchPan": patch_pan,
        "patchTempo": patch_tempo,
    }


def build_prst_from_dump(decoded: bytes, name: str, skeleton_bytes, debug: bool = False) -> bytes:
    """Overlay a live device dump onto a skeleton's content region and
    recompute the checksum. See EXPORT NOTES at the top of this file.
    `skeleton_bytes` is the skeleton's raw file content (see
    resolve_skeleton_bytes), not a path.

    The "(overlaid N of M dump bytes...)" accounting line used to print
    unconditionally, on every call -- meaning once per slot on a batch
    export, 256 extra lines on a full `export --all` with nothing wrong to
    report. Gated behind `debug` as of 2026-09-29, per direct feedback after
    a real `export --all` run: "I also saw a lot of extra output that
    should probably be suppressed unless -d is used." Defaults to False (a
    free function, not a Device method, so it has no `self.debug` of its
    own -- every caller passes its own debug flag through explicitly)."""
    out = bytearray(skeleton_bytes)
    end = min(CONTENT_FILE_START + len(decoded), CHECKSUM_OFF)
    overlay_len = end - CONTENT_FILE_START
    out[CONTENT_FILE_START:end] = decoded[:overlay_len]
    if debug:
        print(f"  (overlaid {overlay_len} of {len(decoded)} dump bytes onto the skeleton; "
              f"anything beyond byte {end} in the file keeps the skeleton's own value)")
    struct.pack_into(">H", out, CHECKSUM_OFF, prst_checksum(out))
    return bytes(out)


# The "tail block": 8 entries x 12-byte stride starting at file offset 1120,
# with the only meaningful bytes at +6/+7 of each entry. Device-owned, not
# patch content -- confirmed this session to change on every SAVE (matching
# what RigSheet's own write-verification independently excludes) AND, later
# the same day, to also change on every plain READ (see RAW_DUMP_IGNORE_OFFSETS
# and read_dump_confirmed). Named as its own constant, rather than inlined
# into VERIFY_IGNORE_OFFSETS, because it's also the thing normalize_export_
# dynamic_fields zeros out below -- one set of magic numbers, two consumers.
TAIL_BLOCK_FILE_OFFSETS = frozenset(
    [1120 + q * 12 + 6 for q in range(8)] +
    [1120 + q * 12 + 7 for q in range(8)]
)

# File offset 0x2E, ONE of the three bytes long grouped together as "the
# slot-mirror bytes" (0x2E, 0x34, 0x90) -- but NOT treated the same as the
# other two for export purposes. Two real, independent Valeton Desktop
# exports of the exact same untouched patch (37-A "Blue Sparkle", 2026-09-27)
# agree exactly: 0x2E is 0x00 in BOTH official exports, while 0x34 and 0x90
# both carry the real slot value (0x90 for 37-A) in BOTH -- i.e. Valeton's
# own exporter zeroes 0x2E specifically and leaves the other two alone. Our
# own file (built from a device dump) carries the real slot value at 0x2E
# too, which is what first surfaced this via a side-by-side export
# comparison the user ran. Two independent, agreeing real exports is strong
# evidence for a deterministic byte value (unlike a noisy read/write
# reliability rate, which needs many trials) -- unlike TAIL_BLOCK_FILE_OFFSETS,
# though, this was found by comparing FILES, not by testing OUR OWN reads or
# writes directly, so it says nothing about what the device does with this
# byte on write; see the note in build_upload_image, which still transmits
# this byte as real content and is NOT changed by this finding.
EXPORT_ZEROED_SLOT_ECHO_OFFSET = 0x2E

# More bytes GP200 Studio's own "Export All" zeroes on export, found the same
# way as EXPORT_ZEROED_SLOT_ECHO_OFFSET -- by comparing files, not by testing
# our own reads/writes -- but from a much larger sample: a full 256-slot
# export from this tool against GP200 Studio's own Export All output for the
# same device, 245 patches in common (2026-09-27). Two single-position bytes,
# 0x3E and 0x40, plus three MORE positions within each of the same 8 "tail
# block" quads TAIL_BLOCK_FILE_OFFSETS already tracks: +5, +10, +11 (on top
# of the existing +6/+7). Kept as its own constant rather than folded into
# TAIL_BLOCK_FILE_OFFSETS, for the same reason EXPORT_ZEROED_SLOT_ECHO_OFFSET
# is separate: TAIL_BLOCK_FILE_OFFSETS was established by directly observing
# OUR OWN reads/writes change those bytes on every read/save; these were
# established purely by comparing exported files. For every position below,
# across all 245 common patches, GP200 Studio's export is 0x00 with ZERO
# exceptions, while this tool's raw (unzeroed) device reads carry a real,
# nonzero value in roughly 4-10% of patches -- the same "Studio always
# blanks it, we sometimes still have live content" signature as 0x2E. Says
# nothing about the write path (see the note on EXPORT_ZEROED_SLOT_ECHO_OFFSET
# and build_upload_image) -- not applied there, only to what export writes.
STUDIO_ADDITIONAL_ZEROED_OFFSETS = frozenset(
    [0x3E, 0x40] +
    [1120 + q * 12 + 5 for q in range(8)] +
    [1120 + q * 12 + 10 for q in range(8)] +
    [1120 + q * 12 + 11 for q in range(8)]
)

# Fields inside the live-dump-covered range (CONTENT_FILE_START..CHECKSUM_OFF)
# that a correct write is NOT expected to reproduce, so verification ignores
# them rather than flagging false failures. All three categories were mapped
# empirically this session, comparing real hardware round trips byte-for-byte:
#   - 0x28-0x2D: a short stamp (seen as literal text like `MRAP...` plus a
#     couple of bytes) that only the Valeton desktop app's own exporter
#     writes -- our upload payload never includes or transmits this range at
#     all (see build_upload_image's CONTENT_START=0x2E), so it can never
#     match whatever the source file happened to have there.
#   - 0x2E, 0x34, 0x90: the slot-mirror bytes -- correctly expected to change
#     to the NEW target slot, not stay equal to the source file's own.
#   - TAIL_BLOCK_FILE_OFFSETS (see above): fields RigSheet's own
#     write-verification also excludes, matching what we've observed --
#     they change on every save regardless of what was written.
VERIFY_IGNORE_OFFSETS = frozenset(
    list(range(0x28, 0x2E)) +
    [0x2E, 0x34, 0x90]
) | TAIL_BLOCK_FILE_OFFSETS


def normalize_export_dynamic_fields(file_bytes: bytes) -> bytes:
    """Zero the tail block (TAIL_BLOCK_FILE_OFFSETS), the 0x2E slot-echo byte
    (EXPORT_ZEROED_SLOT_ECHO_OFFSET), and the further positions GP200
    Studio's own export also blanks (STUDIO_ADDITIONAL_ZEROED_OFFSETS) in an
    exported .prst, then recompute the checksum. Export-only -- NOT used for
    write verification's internal comparisons or its saved failure
    diagnostics, which still want the device's real, unmodified reported
    value.

    Tail block: device-owned scratch state, not patch content -- it changes
    on every save AND on every plain read (see the constant's own
    docstring), so leaving the raw device value in an exported file means the
    exact same patch, read twice back to back with nothing changed, produces
    two different files. That's the property a user asked about directly
    (2026-09-27): comparing our export of an untouched patch against
    Valeton's own official export of the same patch would show a byte
    difference in this region even though nothing is actually different,
    which reads exactly like a bug to someone who doesn't already know about
    this investigation. Two real Valeton exports of the same patch confirm
    they zero this too.

    0x2E: a second, narrower finding from that same side-by-side comparison
    (see EXPORT_ZEROED_SLOT_ECHO_OFFSET) -- two independent real Valeton
    exports of the same untouched patch both show 0x00 here, while both
    still carry the real slot value at the OTHER two "slot-mirror" offsets
    (0x34, 0x90), which this function deliberately leaves untouched. Only
    0x2E gets zeroed.

    STUDIO_ADDITIONAL_ZEROED_OFFSETS: a third finding, from comparing a full
    256-slot export against GP200 Studio's own Export All output across 245
    real patches in common (2026-09-27) -- 0x3E, 0x40, and three more
    positions (+5, +10, +11) within each already-tracked tail-block quad.
    Same "Studio always zeroes it, we sometimes still show live content"
    signature as 0x2E, just confirmed at much larger sample size.

    None of this affects verify_write_full/read_dump_confirmed, which still
    work off the actual bytes the device returns, and section 1 notes the
    device recomputes the tail block on every save regardless of content, so
    a later re-upload of a zeroed export isn't losing anything meaningful
    there either."""
    out = bytearray(file_bytes)
    for off in TAIL_BLOCK_FILE_OFFSETS:
        out[off] = 0x00
    out[EXPORT_ZEROED_SLOT_ECHO_OFFSET] = 0x00
    for off in STUDIO_ADDITIONAL_ZEROED_OFFSETS:
        out[off] = 0x00
    struct.pack_into(">H", out, CHECKSUM_OFF, prst_checksum(out))
    return bytes(out)

# The same ignore set, translated from .prst FILE offsets to offsets within
# a RAW device dump (as Device.read_dump returns it, before
# build_prst_from_dump overlays it onto a skeleton at CONTENT_FILE_START).
# Needed because read_dump_confirmed compares raw dumps directly, without
# ever building a full .prst file -- and a real hardware test (2026-09-27)
# proved it needs this filtering just as much as diff_prst_content does: the
# tail block turns out to change on every plain READ, not just every SAVE as
# previously documented, so comparing it unfiltered made two reads of a
# completely untouched slot look unconfirmable forever (100% of the time),
# not just occasionally -- read_dump_confirmed was comparing raw bytes for
# exact equality with no filtering at all, the one comparison in this
# codebase that had never needed VERIFY_IGNORE_OFFSETS before, because it's
# brand new this session; every earlier verification path went through
# diff_prst_content, which already excluded this.
RAW_DUMP_IGNORE_OFFSETS = frozenset(off - CONTENT_FILE_START for off in VERIFY_IGNORE_OFFSETS)


def raw_dumps_agree(a: bytes, b: bytes) -> bool:
    """True if two raw device dumps agree everywhere that matters for
    confirming a read -- i.e. everywhere except RAW_DUMP_IGNORE_OFFSETS.
    A length mismatch is never considered agreement, regardless of content."""
    if len(a) != len(b):
        return False
    return all(x == y for i, (x, y) in enumerate(zip(a, b)) if i not in RAW_DUMP_IGNORE_OFFSETS)


def diff_prst_content(expected: bytes, actual: bytes):
    """Compare two .prst-shaped buffers over the range a device dump actually
    covers, skipping VERIFY_IGNORE_OFFSETS. Returns a list of
    (offset, expected_byte, actual_byte) for anything else that differs --
    empty means the write reproduced everything it's supposed to."""
    end = min(len(expected), len(actual), CHECKSUM_OFF)
    return [(i, expected[i], actual[i]) for i in range(CONTENT_FILE_START, end)
            if i not in VERIFY_IGNORE_OFFSETS and expected[i] != actual[i]]


def describe_prst_offset(offset: int) -> str:
    """Human-readable label for a .prst byte offset, for verification-failure
    messages -- e.g. 'block 4 param 0' rather than a bare hex offset."""
    if 0x44 <= offset < 0x54:
        return "patch name"
    if 0x54 <= offset < 0x64:
        return "author"
    if 0x64 <= offset < 0x8C:
        return "note"
    if 0x8C <= offset < 0xA0:
        # 20 bytes between the note field and the first effect block whose
        # purpose isn't otherwise mapped this session. Every real .prst
        # sampled so far (official Valeton exports and our own round-trip
        # exports alike) has 0x00 at every byte in here, including the exact
        # byte (0x9F) that a real calibrate-settle run caught intermittently
        # coming back non-zero across several different settle delays -- so
        # treat a mismatch in this range as a real, reproducible finding, not
        # a benign device-owned field like the ones in VERIFY_IGNORE_OFFSETS.
        return f"pre-effects header byte {offset - 0x8C} (0x8C-0x9F, always 0x00 in every real sample seen so far)"
    EFFECT_BLOCK_START = 0xA0
    EFFECT_BLOCK_SIZE = 0x48
    if EFFECT_BLOCK_START <= offset < EFFECT_BLOCK_START + 11 * EFFECT_BLOCK_SIZE:
        rel = offset - EFFECT_BLOCK_START
        blk, within = divmod(rel, EFFECT_BLOCK_SIZE)
        if within == 4:
            return f"block {blk} slotIndex"
        if within == 5:
            return f"block {blk} enabled"
        if 8 <= within < 12:
            return f"block {blk} effectId"
        if within >= 0x0C:
            return f"block {blk} param {(within - 0x0C) // 4}"
        return f"block {blk} byte {within}"
    return f"file offset 0x{offset:04X}"


def _describe_raw_dump_diff(a: bytes, b: bytes, limit: int = 8) -> str:
    """One-line summary of how two RAW device dumps (as returned by
    Device.read_dump, before build_prst_from_dump overlays them onto a
    skeleton) differ -- used by read_dump_confirmed to show exactly what
    changed between two disagreeing reads, in the same file-offset terms
    used everywhere else (describe_prst_offset expects a full .prst file
    offset, so raw dump index i is reported as CONTENT_FILE_START + i).

    Skips RAW_DUMP_IGNORE_OFFSETS, same as raw_dumps_agree: a real hardware
    test found the tail block changes on every plain read, so showing it
    here would bury the differences that actually matter (if any remain
    after that filtering, THOSE are the interesting ones) under noise from
    a field that's expected to change and is never treated as a
    disagreement in the first place."""
    if len(a) != len(b):
        return f"DIFFERENT LENGTH ({len(a)} vs {len(b)} raw bytes)"
    diffs = [(CONTENT_FILE_START + i, x, y) for i, (x, y) in enumerate(zip(a, b))
             if x != y and i not in RAW_DUMP_IGNORE_OFFSETS]
    if not diffs:
        return ("only known device-owned/dynamic bytes differ (e.g. the tail block, which "
                "changes on every read) -- nothing unexpected")
    shown = diffs[:limit]
    detail = "; ".join(f"{describe_prst_offset(off)} (0x{off:04X}): 0x{x:02X} vs 0x{y:02X}"
                        for off, x, y in shown)
    more = f" (+{len(diffs) - limit} more)" if len(diffs) > limit else ""
    return f"{len(diffs)} byte(s) differ -- {detail}{more}"

# --------------------------------------------------------------- device ----

def _describe(full_msg: bytes) -> str:
    """Human-readable one-liner for a message, used by --debug."""
    n = len(full_msg)
    if full_msg[0:8] == HEADER and n > 10:
        cmd, sub = full_msg[8], full_msg[9]
        body = full_msg[10:-1]
        # Read-chunk responses (cmd=0x12/sub=0x18) are shown IN FULL, not
        # truncated to 24 bytes like other traffic: these are exactly the
        # messages under scrutiny for the read-instability investigation
        # (see PROTOCOL_NOTES.md), and truncating them hides the very bytes
        # that matter -- a real, confirmed mismatch has landed well past
        # byte 24 into a 384-byte chunk, invisible at the old truncation.
        if cmd == 0x12 and sub == 0x18:
            return f"GP-200 sysex  cmd=0x{cmd:02X} sub=0x{sub:02X}  {n} bytes  body: {body.hex(' ')}"
        shown = body[:24].hex(" ")
        more = f" ...(+{len(body) - 24} more)" if len(body) > 24 else ""
        return f"GP-200 sysex  cmd=0x{cmd:02X} sub=0x{sub:02X}  {n} bytes  body: {shown}{more}"
    shown = full_msg[:32].hex(" ")
    more = f" ...(+{n - 32} more)" if n > 32 else ""
    return f"non-GP-200 or malformed  {n} bytes: {shown}{more}"


class ReadNotConfirmedError(TimeoutError):
    """Raised by Device.read_dump_confirmed when repeated reads of a slot
    never agreed with each other at all -- i.e. the slot couldn't be read
    with any confidence, as opposed to simply disagreeing with some
    expected value. Subclasses TimeoutError so existing `except
    TimeoutError` call sites (which already mean "couldn't get a
    trustworthy read of this slot") keep working without changes, while
    call sites that want to tell the two apart can catch this specifically
    (list it before a bare `except TimeoutError` to do so)."""
    pass


class Device:
    def __init__(self, port_substr: str | None, debug: bool = False):
        self.debug = debug
        # Session-relative clock for --debug timestamps (2026-09-29, added
        # while chasing why `list` feels much slower than `export --all`
        # despite export doing more work per slot -- see PROTOCOL_NOTES.md).
        # Monotonic, not wall-clock: only elapsed time within this run matters,
        # and monotonic is immune to system clock adjustments mid-run.
        self._t0 = time.monotonic()
        ins = mido.get_input_names()
        outs = mido.get_output_names()
        in_name = self._match(ins, port_substr)
        out_name = self._match(outs, port_substr)
        print(f"Connecting: in={in_name!r}  out={out_name!r}")
        self.inport = mido.open_input(in_name)
        self.outport = mido.open_output(out_name)
        self._handshake()

    def _dbg(self, msg: str):
        """Print a --debug line prefixed with elapsed time since this Device
        connected (see self._t0). Centralizing the timestamp format here,
        rather than re-deriving it at each of the ~10 call sites below, is
        what makes it practical to change later (2026-09-29)."""
        if self.debug:
            print(f"[+{time.monotonic() - self._t0:7.3f}s] {msg}")

    def _handshake(self):
        """Identity query + enter-editor-mode, matching what the reference
        editor always does right after opening the port. See
        build_enter_editor_mode's docstring for why this matters for writes."""
        try:
            self.send(build_identity_query())
            got = self._drain_matching(0x12, 0x08, 1, READ_TIMEOUT_S)
            if not got:
                self._dbg("warning: no identity response within timeout; continuing anyway")
        except Exception as e:
            self._dbg(f"warning: identity query failed ({e}); continuing anyway")
        self.send(build_enter_editor_mode())
        time.sleep(0.1)

    @staticmethod
    def _match(names, substr):
        if substr:
            matches = [n for n in names if substr.lower() in n.lower()]
        else:
            matches = [n for n in names if "gp" in n.lower() and "200" in n.lower()]
        if len(matches) == 1:
            return matches[0]
        print("Available MIDI ports:")
        for n in names:
            print(f"  {n}")
        if not matches:
            sys.exit("Could not find a GP-200 port automatically. "
                      "Close Valeton's app and GP200 Studio, then re-run with --port "
                      "and part of the exact name shown above.")
        sys.exit(f"Multiple ports matched; re-run with --port and one of: {matches}")

    def close(self):
        self.inport.close()
        self.outport.close()

    def send(self, full_msg: bytes):
        self._dbg(f"-> {_describe(full_msg)}")
        # mido wants the data BETWEEN F0 and F7, not the framing bytes themselves.
        self.outport.send(mido.Message("sysex", data=full_msg[1:-1]))

    def _flush_pending(self):
        """Discard any messages already sitting in the input queue. Right
        after a write burst the device can still be emitting trailing
        traffic that happens to share an opcode with real read responses
        (see require_offset0 below); flushing before a fresh request keeps
        that stale traffic from being mistaken for the answer to it."""
        n = 0
        while self.inport.receive(block=False) is not None:
            n += 1
        if n:
            self._dbg(f"(flushed {n} pending message(s) before sending)")

    def _drain_matching(self, cmd, sub, want, timeout_s, require_offset0=False):
        # started/elapsed here is the actual measured round-trip for THIS
        # request, distinct from self._t0 (session-wide, used by _dbg's
        # per-line prefix) -- added 2026-09-29 to compare name-only vs
        # full-dump request latency directly instead of eyeballing deltas
        # between _dbg's timestamps by hand (see PROTOCOL_NOTES.md).
        started = time.monotonic()
        chunks = []
        seen_offsets = set()
        seen_other = 0
        deadline = started + timeout_s
        while time.monotonic() < deadline:
            msg = self.inport.receive(block=False)
            if msg is None:
                time.sleep(0.01)
                continue
            if msg.type != "sysex":
                self._dbg(f"<- non-sysex MIDI message: {msg}")
                continue
            full = bytes([0xF0, *msg.data, 0xF7])
            if is_sysex(full, cmd, sub):
                offset_key = (full[11], full[12])
                if require_offset0 and offset_key != (0, 0):
                    # A genuine single-chunk (name-only) response is always
                    # offset 0. Anything else with the same cmd/sub is stray
                    # traffic -- e.g. an echo of a write chunk still working
                    # its way through the device right after a flash write --
                    # and must not be mistaken for the read we asked for.
                    self._dbg(f"<- {_describe(full)}  [matched cmd/sub but offset "
                              f"({full[11]}/{full[12]}) != 0 -- ignoring stray chunk]")
                    continue
                if offset_key in seen_offsets:
                    # A repeat of an offset we already have is stray/duplicate
                    # traffic (e.g. a lingering echo), not a legitimate part
                    # of a multi-chunk read -- accepting it would either
                    # double-count toward `want` or silently shadow the real
                    # chunk for that offset.
                    self._dbg(f"<- {_describe(full)}  [duplicate offset "
                              f"({full[11]}/{full[12]}) -- ignoring]")
                    continue
                seen_offsets.add(offset_key)
                self._dbg(f"<- {_describe(full)}  [MATCH, chunk {len(chunks) + 1}/{want}]")
                chunks.append(full)
                if len(chunks) == want:
                    self._dbg(f"(all {want} chunk(s) received in {time.monotonic() - started:.3f}s)")
                    return chunks
            else:
                seen_other += 1
                self._dbg(f"<- {_describe(full)}  [did not match expected cmd=0x{cmd:02X} sub=0x{sub:02X}]")
        if not chunks:
            self._dbg(f"(timed out after {time.monotonic() - started:.3f}s of {timeout_s}s budget; "
                      f"{seen_other} other sysex message(s) seen, 0 matched)")
        return chunks if len(chunks) == want else None

    def read_name(self, slot: int, retries=RETRY_COUNT) -> str:
        for attempt in range(retries + 1):
            self._flush_pending()
            self.send(build_read_request(slot, name_only=True))
            chunks = self._drain_matching(0x12, 0x18, 1, READ_TIMEOUT_S, require_offset0=True)
            if chunks:
                return parse_preset_name(chunks[0])
        raise TimeoutError(f"no response reading name of slot {slot_to_label(slot)}")

    def read_name_via_dump(self, slot: int, retries=RETRY_COUNT) -> str:
        """Same result as read_name(), but by asking for the full dump (the
        same request export/read_dump uses) instead of the name-only
        (sub=0x20) request, and keeping only the name.

        Real-hardware evidence (2026-09-29, see PROTOCOL_NOTES.md) showed
        these two request types behave completely differently on this
        firmware: across every slot in two full traces, the name-only
        request's FIRST attempt timed out the full READ_TIMEOUT_S with zero
        exceptions (succeeding only on the automatic retry, ~15ms later),
        while the full-dump request succeeded on its first attempt in under
        20ms with zero exceptions. `list` switched to this method because of
        that -- not because of any doubt about read_name's correctness, but
        because read_name reliably pays a ~READ_TIMEOUT_S tax read_dump
        never does, for reasons still unconfirmed (see the "in progress"
        note in PROTOCOL_NOTES.md).

        Deliberately uses plain read_dump, not read_dump_confirmed: a wrong
        name once in a rare while is cosmetic and just means re-reading that
        one slot, nothing like the corruption risk read_dump_confirmed
        exists to catch for real backups."""
        decoded = self.read_dump(slot, retries=retries)
        return extract_name_field(decoded)

    def read_dump(self, slot: int, retries=RETRY_COUNT) -> bytes:
        for attempt in range(retries + 1):
            self._flush_pending()
            self.send(build_read_request(slot, name_only=False))
            chunks = self._drain_matching(0x12, 0x18, 7, READ_TIMEOUT_S)
            if chunks:
                return assemble_chunks(chunks)
        raise TimeoutError(f"no response reading slot {slot_to_label(slot)} (expected 7 chunks)")

    def read_dump_confirmed(self, slot: int, tries: int = 5) -> bytes:
        """Like read_dump, but re-reads up to `tries` times (minimum 2,
        default 5) and returns as soon as ANY two of those reads agree
        (via raw_dumps_agree, which ignores RAW_DUMP_IGNORE_OFFSETS --
        see that function) -- not just two in a row. If none of the `tries`
        reads ever agree with each other, raises ReadNotConfirmedError
        rather than guessing.

        Default raised 3 -> 5 (2026-09-27) after a real 256-slot `export
        --all` run hit "3 reads never agreed" on 9/256 slots (~3.5%) --
        annoying at that scale even though each case is just a skipped
        backup, not corrupted data. The math explains why: confirmation
        needs only 2 *clean* reads to agree (all clean reads see the same
        true content), so "never agree" requires at most 1 clean read out
        of `tries`. With the independently measured ~12-15%-per-read glitch
        rate, P(<=1 clean out of 3) works out to roughly 5%, matching what
        was observed; P(<=1 clean out of 5) is roughly 0.1% -- about a 50x
        drop -- because getting 4-or-5 bad reads out of 5 is far rarer than
        getting 2-or-3 bad out of 3. This costs nothing in the common case:
        the loop still returns as soon as any two reads agree, so slots that
        confirm in 2 reads (the large majority) are unaffected; only the
        genuinely glitchy slots pay for the extra attempts. See
        PROTOCOL_NOTES.md finding 11 for the full derivation. (There's a
        smaller residual risk this doesn't address: if two independently
        glitched reads happened to land on the exact same wrong byte value,
        they'd wrongly "agree." Observed glitch values at the known
        trouble spot cluster around a handful of repeating bytes rather
        than being fully random, so this isn't impossible -- just far less
        likely than the dominant failure mode this fix targets, since any
        specific wrong value is much rarer than the correct one.)

        IMPORTANT (found via real hardware testing, 2026-09-27): this
        compares using raw_dumps_agree, NOT raw `==`. A real test showed
        that comparing raw dumps for exact byte equality made confirmation
        fail 100% of the time, even reading a completely untouched slot with
        zero writes anywhere in the session -- because the "tail block"
        (see VERIFY_IGNORE_OFFSETS) turns out to change on every plain READ,
        not just every save as previously documented. diff_prst_content
        already knew to ignore this region; this method didn't, because it's
        a new comparison axis (raw dump vs raw dump) that didn't exist
        before today. Comparing raw bytes unfiltered here made two reads of
        an unchanged slot look permanently unconfirmable, which is a
        different and much worse failure mode than the read-noise problem
        this method was built to solve in the first place.

        This exists because of a real, confirmed finding (see the `reread`
        command and PROTOCOL_NOTES.md): a bare read can occasionally return
        one byte wrong even though NOTHING was written to the device in
        between -- proof this is a host MIDI-stack receive glitch, not a
        device-storage problem. Treating a lone read as ground truth means
        roughly 1 in 7 reads (the measured rate) risks silently corrupting
        an export, or -- worse -- being mistaken for a failed write by code
        that reads back to verify one.

        Checking every prior read (not just the immediately preceding one)
        matters: with reads [A, B, A], the first and third agree even
        though nothing CONSECUTIVE does, and that's real signal worth
        catching rather than ignoring, especially with the small `tries`
        budgets used here. And when nothing ever agrees -- [A, B, C], all
        different -- the honest answer is "couldn't get a trustworthy read
        of this slot," not a silent guess. An earlier version of this
        method returned its last attempt in that case with no way for the
        caller to tell it apart from a confirmed read; every caller here
        (write verification, export, diag-write's backup) cares more about
        knowing it couldn't get a trustworthy read than about getting
        *some* answer regardless of confidence, so this raises instead.

        Used by verify_write_full and by export, both of which care about
        byte-for-byte accuracy more than saving one or two extra ~1-2s
        reads.

        These retry-diagnostic lines used to print unconditionally --
        deliberately NOT gated behind --debug, on the theory that a
        disagreeing read is exactly the situation that's hard to reason
        about blind. In practice, on a real `export --all` (256 slots, each
        one calling this with tries=5), that theory produced a genuinely
        noisy normal-case console: nothing was wrong, but the retry-glitch
        rate discussed above (~12-15% per read) meant a meaningful fraction
        of slots printed 2+ extra lines apiece even on a fully successful
        run. Changed 2026-09-29 to gate behind `self.debug` instead, per
        direct feedback: "I also saw a lot of extra output that should
        probably be suppressed unless -d is used." The detail is still
        there for anyone who needs it -- just behind --debug now, same as
        everything else diagnostic."""
        if tries < 2:
            raise ValueError("read_dump_confirmed needs at least 2 tries")
        seen = []
        for i in range(1, tries + 1):
            cur = self.read_dump(slot)
            for j, prior in enumerate(seen, start=1):
                if raw_dumps_agree(cur, prior):
                    if i > 2:
                        # Took more than the trivial first-two-agree case --
                        # worth knowing on an otherwise silent success path,
                        # but only when --debug is on (see docstring above).
                        self._dbg(f"read_dump_confirmed({slot_to_label(slot)}): read {i} "
                                  f"matches read {j} -- confirmed after {i} attempt(s)")
                    return cur
            if i > 1:
                # Didn't match anything seen so far -- show exactly how it
                # differs from every prior attempt (see docstring above for
                # why this is gated behind --debug now, not unconditional).
                for j, prior in enumerate(seen, start=1):
                    self._dbg(f"read_dump_confirmed({slot_to_label(slot)}): "
                              f"read {i} vs read {j}: {_describe_raw_dump_diff(prior, cur)}")
            seen.append(cur)
        raise ReadNotConfirmedError(
            f"{tries} read(s) of {slot_to_label(slot)} never agreed with "
            "each other -- couldn't get a trustworthy read")

    def write_slot(self, slot: int, file_bytes: bytes, currently_active: int | None,
                   commit: bool = False, settle_s: float = 1.0):
        """The flash-chunk upload path (matches GP200 Studio's pushPreset).
        Its own project history flags this as never fully proven reliable;
        `commit` is their own unresolved experimental workaround -- see
        build_save_commit's docstring for the risk it carries. `settle_s` is
        the pause after the chunk burst before anything else is sent -- see
        cmd_calibrate_settle, which sweeps this value directly to test
        whether retries are actually a flash-commit-timing issue."""
        image = build_upload_image(file_bytes, slot)
        chunks = build_upload_chunks(image, slot)
        if currently_active == slot:
            park = slot ^ 1
            print(f"  parking on {slot_to_label(park)} while writing the active slot")
            self.send(build_preset_change(park))
            time.sleep(0.3)
        for i, c in enumerate(chunks):
            self._dbg(f"-> chunk {i + 1}/{len(chunks)}: {_describe(c)}")
            self.outport.send(mido.Message("sysex", data=c[1:-1]))
            # There's no ACK/NAK on this write path -- the protocol gives no
            # per-chunk confirmation at all (confirmed against both reference
            # projects; they blast the same way). 40ms matches RigSheet's own
            # spacing, a bit more conservative than the 20ms this used to be,
            # as cheap insurance against outrunning the device -- it's not a
            # substitute for the full-content verify/retry in write_and_verify,
            # just makes needing a retry less likely.
            time.sleep(0.04)
        # Let the flash write settle before doing anything else -- hardware
        # testing (both ours and the reference project's) shows the device
        # ignores commands sent too soon after the chunk burst.
        time.sleep(settle_s)
        if commit:
            name = prst_file_name(file_bytes)
            self._dbg(f"-> sending experimental save-commit finalize for {name!r}")
            self.send(build_save_commit(name, slot))
            time.sleep(0.4)
        # Explicitly select the target slot -- the reference editor always
        # does this after every push, not just when it had to park first --
        # it's what actually loads the freshly written flash copy into the
        # device's active/displayed state.
        self.send(build_preset_change(slot))
        time.sleep(0.3)

    def write_slot_live(self, slot: int, preset: dict):
        """The 'supported' path per GP200 Studio's own git history: replay a
        decoded preset as individual live-parameter edits (effect type, each
        of its 15 params, on/off state, chain order, patch-level VOL/PAN/
        TEMPO, author), then a save-commit. Slower -- a few hundred small
        messages instead of 7 big ones -- but has been the one write path
        the project has trusted since its very first commit."""
        self.send(build_preset_change(slot))
        time.sleep(0.2)
        for eff in preset["effects"]:
            si = eff["slotIndex"]
            self._dbg(f"-> block {si}: effect 0x{eff['effectId']:08X}, "
                      f"{'on' if eff['enabled'] else 'off'}")
            self.send(build_effect_change(si, eff["effectId"]))
            time.sleep(0.03)
            for p, value in enumerate(eff["params"]):
                self.send(build_param_change(si, p, eff["effectId"], value))
                time.sleep(0.008)
            self.send(build_toggle_effect(si, eff["enabled"]))
            time.sleep(0.015)
        time.sleep(0.05)
        order = [eff["slotIndex"] for eff in preset["effects"]]
        self.send(build_reorder_effects(order, preset["fxLoopSend"], preset["fxLoopReturn"]))
        time.sleep(0.03)
        self.send(build_patch_setting(0x00, preset["patchVolume"]))
        time.sleep(0.03)
        self.send(build_patch_setting(0x06, preset["patchPan"] & 0xFF))
        time.sleep(0.03)
        self.send(build_patch_setting(0x01, preset["patchTempo"]))
        time.sleep(0.03)
        if preset.get("author"):
            self.send(build_author_name(preset["author"]))
            time.sleep(0.03)
        self.send(build_save_commit(preset["patchName"], slot))
        time.sleep(0.3)

    def verify_write(self, slot: int, expected_name: str):
        """Read the name back and compare against what was uploaded.
        Returns (ok: bool, actual_name: str)."""
        try:
            actual = self.read_name(slot)
        except TimeoutError:
            return False, "(no response)"
        return actual.strip() == expected_name.strip(), actual

    def verify_write_full(self, slot: int, file_bytes: bytes, skeleton_bytes):
        """Full-content verification: reads the whole slot back and compares
        every field the upload actually controls against the source file,
        ignoring only what's device-owned (see VERIFY_IGNORE_OFFSETS). This
        exists because the write protocol itself has no per-chunk ACK/NAK --
        neither this script nor either reference project has found one --
        so a dropped or garbled byte in the chunk burst has nothing to catch
        it except checking the result afterward.

        Uses read_dump_confirmed rather than a single read_dump: a real test
        (the `reread` command, 2026-09-27) proved that a bare read can
        return one byte wrong even with NOTHING written to the device in
        between -- a host MIDI-stack receive glitch, not a write problem.
        Verifying with a single unconfirmed read would misattribute that
        glitch to the write and trigger a pointless retry of a write that
        was already correct; write_and_verify's retry loop should be
        reserved for writes that are actually, repeatably wrong.

        Returns (ok, mismatches, name_on_device, roundtrip_bytes) --
        roundtrip_bytes is the full reconstructed .prst as actually read
        back (or None on no response), so a caller can save the exact
        failed readback to disk for later comparison instead of just
        reporting that it was wrong."""
        try:
            dump = self.read_dump_confirmed(slot)
        except ReadNotConfirmedError:
            return False, None, "(reads never agreed with each other)", None
        except TimeoutError:
            return False, None, "(no response)", None
        roundtrip = build_prst_from_dump(dump, "verify", skeleton_bytes, debug=self.debug)
        mismatches = diff_prst_content(file_bytes, roundtrip)
        return (len(mismatches) == 0), mismatches, prst_file_name(roundtrip), roundtrip

# ---------------------------------------------------------------- zip ------

def write_zip(entries: dict, out_path: Path):
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)


def _parse_leading_slot_label(entry_name: str):
    """If `entry_name` starts with a slot label followed by a space or an
    underscore (e.g. this tool's own `export`/`export --all` naming,
    `37A_Template.prst`, or a hand-named file like `36-A JImi.prst`), return
    that slot number; otherwise None. Used only to ORDER zip entries
    correctly -- never to pick a destination slot, which always comes from
    the trailing destination argument on the `upload` command line (see
    expand_import_sources). This is deliberately a narrow, best-effort
    heuristic: a generic filename's first token before a space/underscore
    (e.g. `Friedman_BE100.prst` -> `Friedman`, `JCM800 Recipe.prst` ->
    `JCM800`) will almost never happen to also be a valid slot label, because
    label_to_slot requires it to end in A/B/C/D -- but it's not impossible,
    so this is a convenience for zips built by this tool's own export or
    named the way the Valeton desktop app does, not a guarantee for an
    arbitrary zip someone hand-assembles."""
    stem = Path(entry_name).stem
    prefix = re.split(r"[\s_]", stem, maxsplit=1)[0]
    try:
        return label_to_slot(prefix)
    except ValueError:
        return None


def expand_import_sources(files: list) -> list:
    """Turns the `upload` command's file arguments (its `targets`, with the
    trailing destination slot already peeled off by cmd_upload) into an
    ordered list of (display_name, file_bytes) pairs -- either straight from
    a plain list of .prst paths (unchanged from the original behavior), or
    unpacked from a single .prst archive (.zip) so a large bulk restore can
    be one file instead of a long, typo-prone command line.

    Order matters here: cmd_upload assigns files[i] to slot `start + i`
    (start being that trailing destination argument), so the order this
    returns IS the destination order, but the destination RANGE always comes
    from that argument -- this never picks a slot on its own, only an order.
    For a zip, that order is: if EVERY entry's filename starts with a valid
    slot label (see _parse_leading_slot_label, matching what this tool's own
    export already names things), sort numerically by
    that label -- correct even though slot labels don't sort right as plain
    text ('8A' would otherwise sort after '37A'). Otherwise (any entry
    lacks a recognized label), fall back to a plain alphabetical sort by
    filename, same ambiguity a plain file list already has today, just
    packaged as a zip instead of separate command-line arguments.

    A zip's contents can perfectly well be aimed at a DIFFERENT range of
    slots than they came from -- e.g. reassigning/reordering patches, the
    way Valeton Desktop's own drag-and-drop slot reordering does -- so an
    embedded label is read only to make that ordering deterministic and
    correct, never to override the range the caller actually asked for."""
    zips = [f for f in files if f.lower().endswith(".zip")]
    if zips and len(files) > 1:
        sys.exit("can't mix a .zip archive with individual .prst files -- "
                  "pass exactly one .zip, or a plain list of .prst files, not both")
    if zips:
        archive_path = zips[0]
        with zipfile.ZipFile(archive_path) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".prst")]
            skipped = [n for n in zf.namelist() if not n.lower().endswith(".prst") and not n.endswith("/")]
            for n in skipped:
                print(f"  {archive_path}: skipping {n} (not a .prst file)")
            if not names:
                sys.exit(f"{archive_path} has no .prst files in it")
            entries = [(n, zf.read(n)) for n in names]
        labels = [_parse_leading_slot_label(n) for n, _ in entries]
        if all(l is not None for l in labels):
            entries.sort(key=lambda e: _parse_leading_slot_label(e[0]))
            print(f"  {archive_path}: {len(entries)} .prst file(s) found, "
                  f"ordered by their embedded slot label")
        else:
            entries.sort(key=lambda e: e[0].lower())
            print(f"  {archive_path}: {len(entries)} .prst file(s) found, "
                  f"ordered alphabetically (not every filename has a recognized slot label)")
        return entries
    return [(f, Path(f).read_bytes()) for f in files]

# ------------------------------------------------------------- confirm -----

def confirm_overwrite(dev: Device, slot: int, force: bool) -> bool:
    try:
        existing = dev.read_name(slot)
    except TimeoutError as e:
        print(f"  warning: couldn't read {slot_to_label(slot)} before writing ({e})")
        existing = "?"
    print(f"  {slot_to_label(slot)} currently: {existing!r}")
    if force:
        return True
    ans = input(f"  overwrite {slot_to_label(slot)} ({existing!r})? [y/N] ").strip().lower()
    return ans == "y"


def confirm_overwrite_file(path: Path, force: bool) -> bool:
    if not path.exists():
        return True
    if force:
        return True
    ans = input(f"  {path} already exists -- overwrite? [y/N] ").strip().lower()
    return ans == "y"

# --------------------------------------------------------------- verbs -----

def cmd_list_ports(args):
    print("Inputs:")
    for n in mido.get_input_names():
        print(f"  {n}")
    print("Outputs:")
    for n in mido.get_output_names():
        print(f"  {n}")


def cmd_list(args):
    dev = Device(args.port, debug=args.debug)
    try:
        started = time.monotonic()
        timeouts = 0
        for slot in range(TOTAL_SLOTS):
            try:
                # read_name_via_dump, not read_name -- see its docstring for
                # the real-hardware evidence (2026-09-29) that the name-only
                # request this used to use pays a ~READ_TIMEOUT_S tax on
                # every single slot that the full-dump request never does.
                name = dev.read_name_via_dump(slot)
            except TimeoutError:
                name = "(no response)"
                timeouts += 1
            print(f"{slot_to_label(slot):>4}  {name}")
        # Unconditional, not --debug-only: a one-line total is cheap to print
        # and directly answers "how long did this actually take" without
        # needing a debug trace -- added 2026-09-29 while comparing `list`'s
        # real-world speed against `export --all` (see PROTOCOL_NOTES.md).
        elapsed = time.monotonic() - started
        print(f"\n{TOTAL_SLOTS} slots read in {elapsed:.1f}s"
              + (f" ({timeouts} timeout(s))" if timeouts else ""))
    finally:
        dev.close()


def cmd_read(args):
    slot = label_to_slot(args.slot)  # validate before opening the connection
    dev = Device(args.port, debug=args.debug)
    try:
        print(f"{args.slot} -> {dev.read_name(slot)!r}")
    finally:
        dev.close()


def cmd_export(args):
    """Reads use read_dump_confirmed (re-reads until two agree), not a bare
    read_dump -- a real test (the `reread` command, 2026-09-27) proved a
    single read can return one byte wrong even with nothing written to the
    device, at a measured rate of roughly 1 in 7. For a tool whose whole
    point is an accurate backup, trusting a single unconfirmed read would
    mean routinely risking silent, undetected corruption in the saved file
    -- the opposite of what export is for.

    Three mutually exclusive modes (2026-09-27: added --start/--end,
    mirroring apply-template's existing range syntax and its parse_slot_range
    helper): a single slot -> one .prst; --all -> every one of the 256 slots
    into a zip; --start/--end -> just that inclusive range into a zip. The
    range mode exists because --all reads and writes every slot even when
    only a handful are actually wanted -- e.g. re-verifying a just-uploaded
    batch, which used to mean either a full 256-slot export followed by
    manually deleting the unwanted files, or one `export` invocation per
    slot."""
    try:
        skeleton = resolve_skeleton_bytes(args.skeleton)
    except (FileNotFoundError, ValueError) as e:
        sys.exit(f"Couldn't load a skeleton .prst ({e}). Pass --skeleton path/to/any.prst "
                  "(any real exported patch works).")

    range_given = args.start is not None or args.end is not None
    if range_given and not (args.start and args.end):
        sys.exit("--start and --end must be given together")
    modes_given = sum([bool(args.slot), args.all, range_given])
    if modes_given == 0:
        sys.exit("export needs a slot, or --all, or --start/--end -- e.g. "
                  "'export 34-B', 'export --all', or 'export --start 34-A --end 36-D'")
    if modes_given > 1:
        sys.exit("give exactly one of: a single slot, --all, or --start/--end")

    if range_given:
        slots = parse_slot_range(args.start, args.end)  # validate before opening the connection
    elif args.all:
        slots = list(range(TOTAL_SLOTS))
    else:
        label_to_slot(args.slot)  # validate before opening the connection
        slots = None  # single-slot path, below

    if slots is not None:
        # The output filename never depends on anything read from the device
        # -- it's either --out or a fixed/derived default -- so confirm it up
        # front, before spending time reading every slot in the batch, rather
        # than after. A real ~256-slot run (2026-09-27) showed the old order:
        # the overwrite prompt only appeared at the very end, so a single "n"
        # (or an accidental keystroke) threw away a full read pass.
        if args.out:
            out_path = Path(args.out)
        elif args.all:
            out_path = Path("gp200_all_patches.zip")
        else:
            out_path = Path(f"gp200_{args.start}_to_{args.end}.zip")
        if not confirm_overwrite_file(out_path, args.force):
            print("  skipped (nothing done)")
            return

    dev = Device(args.port, debug=args.debug)
    try:
        if slots is not None:
            entries = {}
            skipped_labels = []
            ir_nam_by_label = {}
            started = time.monotonic()
            for slot in slots:
                label = slot_to_label(slot)
                try:
                    decoded = dev.read_dump_confirmed(slot)
                    name = extract_name_field(decoded) or label
                    print(f"{label}: {name!r} ({len(decoded)} bytes)")
                    deps = find_ir_nam_dependencies(decoded)
                    if deps:
                        ir_nam_by_label[label] = deps
                    data = normalize_export_dynamic_fields(
                        build_prst_from_dump(decoded, name, skeleton, debug=args.debug))
                    entries[f"{label}_{safe_filename(name)}.prst"] = data
                except TimeoutError as e:
                    print(f"{label}: skipped ({e})")
                    skipped_labels.append(label)
            elapsed = time.monotonic() - started
            write_zip(entries, out_path)
            # Same unconditional total as `list` prints (2026-09-29) -- these
            # two commands' per-slot timing are being compared directly, so
            # both need to report it the same way without requiring --debug.
            print(f"Wrote {len(entries)} patches to {out_path}  "
                  f"({len(slots)} slots read in {elapsed:.1f}s)")
            # A zip has no concept of "empty slot" -- it's just however many
            # entries got written. `upload` later fills slots consecutively
            # from wherever ITS destination argument says, in zip order --
            # it never looks at the skipped slot's original position (see
            # upload's own help text). So a gap here isn't preserved on
            # restore: it silently compacts, and every patch after the gap
            # ends up one slot earlier than where it actually came from.
            # Surface that now, while the user still knows which slot(s)
            # timed out, rather than as a mystery after a later restore.
            if skipped_labels:
                print(f"WARNING: {len(skipped_labels)} slot(s) were skipped and are NOT in "
                      f"this zip: {', '.join(skipped_labels)}. If you 'upload' this zip back "
                      "later, patches pack consecutively from the destination slot onward -- "
                      "the gap is NOT preserved, so every patch after it would land ONE SLOT "
                      "EARLIER than where it actually came from. Re-run export to fill the "
                      "gap(s) before using this zip to restore.")
            # This backup only carries the model code that means "whatever's in
            # User-IR/SnapTone slot N", never the IR/NAM content itself (see
            # PROTOCOL_NOTES.md Finding 11) -- so restoring these patches later
            # sounds right only on the same device, with nothing reloaded into
            # those slots since. Flag it now, while it's still actionable,
            # rather than as a "why does this patch sound different" surprise
            # after a factory reset or a move to a new unit.
            if ir_nam_by_label:
                total_refs = sum(len(v) for v in ir_nam_by_label.values())
                print(f"NOTE: {len(ir_nam_by_label)} patch(es) reference {total_refs} "
                      "User-IR/SnapTone(NAM) slot(s) -- this backup only stores WHICH slot, "
                      "not the IR/NAM content itself:")
                for label, deps in ir_nam_by_label.items():
                    print(f"  {label}: {', '.join(deps)}")
                print("  Restoring these on a DIFFERENT device, or after that slot's IR/NAM "
                      "has been reloaded with something else, will sound different -- with no "
                      "warning either way. See PROTOCOL_NOTES.md (Finding 11) / the README's "
                      "'Known limitations' section.")
        else:
            slot = label_to_slot(args.slot)
            try:
                decoded = dev.read_dump_confirmed(slot)
            except TimeoutError as e:
                sys.exit(f"Couldn't export {args.slot}: {e}")
            name = extract_name_field(decoded) or args.slot
            print(f"{args.slot}: {name!r} ({len(decoded)} bytes)")
            deps = find_ir_nam_dependencies(decoded)
            if deps:
                print(f"NOTE: this patch references {', '.join(deps)} -- this backup only "
                      "stores WHICH slot, not the IR/NAM content itself. Restoring it on a "
                      "DIFFERENT device, or after that slot's IR/NAM has been reloaded with "
                      "something else, will sound different -- with no warning either way. "
                      "See PROTOCOL_NOTES.md (Finding 11) / the README's 'Known limitations' "
                      "section.")
            data = normalize_export_dynamic_fields(
                build_prst_from_dump(decoded, name, skeleton, debug=args.debug))
            out_path = Path(args.out) if args.out else Path(f"{args.slot}_{safe_filename(name)}.prst")
            if not confirm_overwrite_file(out_path, args.force):
                print("  skipped")
                return
            out_path.write_bytes(data)
            print(f"Wrote {out_path}")
    finally:
        dev.close()


def do_write(dev: Device, slot: int, file_bytes: bytes, currently_active, method: str, commit: bool):
    """Dispatch to whichever write strategy was asked for. 'flash' is the
    whole-file chunk upload (GP200 Studio's pushPreset -- fast, never fully
    proven reliable); 'live' decodes the file and replays it as individual
    parameter edits (GP200 Studio's writePresetToSlot -- slower, the one
    path the project has trusted since day one)."""
    if method == "live":
        preset = decode_prst(file_bytes)
        dev.write_slot_live(slot, preset)
    else:
        dev.write_slot(slot, file_bytes, currently_active, commit=commit)


MAX_WRITE_ATTEMPTS = 10

def write_and_verify(dev: Device, slot: int, file_bytes: bytes, method: str, commit: bool,
                      skeleton_bytes):
    """Write a slot, then fully verify it (see Device.verify_write_full), and
    automatically redo the whole write up to MAX_WRITE_ATTEMPTS times if
    anything besides the known device-owned fields comes back wrong. This is
    the compensating control for there being no ACK/NAK on the write path
    itself.

    A real calibrate-settle run (see cmd_calibrate_settle) showed that a
    longer post-write settle delay does NOT reliably prevent this: failures
    recurred at 1.10s, 1.20s and 1.30s, not just at the 1.00s default, so
    settle time alone isn't the (whole) explanation, and this code no longer
    assumes it is. What that same run did show is that every single mismatch,
    at every delay, landed on the exact same file offset -- a specific,
    reproducible weak point rather than scattered random corruption -- and
    that one immediate retry fixed it every single time it happened. Given
    that, the constant here favors giving the retry loop plenty of room
    (attempts are cheap -- a few seconds each) over trying to tune the delay:
    MAX_WRITE_ATTEMPTS is set well above the 1-2 retries actually seen in
    practice so far, rather than chasing a "correct" settle_s. Returns (ok,
    mismatches, name_on_device, attempts_used, roundtrip_bytes) --
    roundtrip_bytes is the last readback (see Device.verify_write_full),
    useful for saving the exact failed state to disk on a final failure."""
    mismatches, name, roundtrip = [], None, None
    for attempt in range(1, MAX_WRITE_ATTEMPTS + 1):
        do_write(dev, slot, file_bytes, None, method, commit)
        ok, mismatches, name, roundtrip = dev.verify_write_full(slot, file_bytes, skeleton_bytes)
        if ok:
            return True, [], name, attempt, roundtrip
        if attempt < MAX_WRITE_ATTEMPTS:
            n = len(mismatches) if mismatches else "?"
            detail = _format_mismatches(mismatches) if mismatches else f"    {name}"
            # Logged every time, not just on final failure -- this is the
            # evidence trail for root-causing *why* retries are needed at
            # all (same field every time? always near the end of the dump?
            # random?), not just proof that the safety net caught something.
            print(f"  verification found {n} mismatched field(s) on attempt {attempt}/"
                  f"{MAX_WRITE_ATTEMPTS} -- retrying the write...\n{detail}")
            time.sleep(0.5)
    return False, (mismatches or []), name, MAX_WRITE_ATTEMPTS, roundtrip


def _format_mismatches(mismatches, limit=6):
    shown = mismatches[:limit]
    lines = [f"    {describe_prst_offset(off)} (0x{off:04X}): expected 0x{exp:02X}, device has 0x{act:02X}"
             for off, exp, act in shown]
    if len(mismatches) > limit:
        lines.append(f"    ...and {len(mismatches) - limit} more")
    return "\n".join(lines)


def _resolve_verify_skeleton():
    try:
        return resolve_skeleton_bytes(None)
    except (FileNotFoundError, ValueError) as e:
        sys.exit(f"Couldn't load a skeleton .prst, needed to verify the write ({e})")


def _print_environment_info():
    """Environment fingerprint -- Python/OS/mido/rtmidi versions and the MIDI
    backend actually in use -- printed as part of a hard-failure report.
    Worth having on hand because this class of intermittent SysEx issue is
    plausibly a host-side MIDI stack quirk rather than the GP-200 itself:
    python-rtmidi's Windows backend uses a small number of fixed-size
    preallocated buffers for incoming SysEx via the Windows Multimedia API,
    which is a documented source of dropped/garbled SysEx independent of
    whatever device is on the other end -- see
    https://github.com/SpotlightKid/python-rtmidi/issues/200 and
    https://github.com/mido/mido/issues/207. Knowing exactly which OS/
    backend/library versions were in play is what makes a failure comparable
    across different computers later."""
    print(f"  python:          {platform.python_version()} ({platform.python_implementation()})")
    print(f"  OS:              {platform.platform()}")
    try:
        print(f"  mido:            {mido.__version__}")
    except Exception:
        pass
    try:
        print(f"  mido backend:    {mido.backend.module.__name__}")
    except Exception:
        pass
    try:
        import rtmidi
        print(f"  python-rtmidi:   {rtmidi.__version__}")
    except Exception:
        pass


class _Tee:
    """Writes everything to both an already-open stream and a log file.
    Used to make --log-file a total capture of a run (see _start_run_log)
    without threading a second "also write this" call through every one of
    this script's many existing print() calls -- they're the log, verbatim,
    at whatever verbosity -d/--debug was already asking for."""
    def __init__(self, primary, secondary):
        self.primary = primary
        self.secondary = secondary

    def write(self, data):
        self.primary.write(data)
        self.secondary.write(data)
        return len(data)

    def flush(self):
        self.primary.flush()
        self.secondary.flush()

    def isatty(self):
        return self.primary.isatty()


def _start_run_log(path_arg: str | None) -> Path:
    """--log-file support (2026-09-27). Redirects stdout AND stderr through
    _Tee for the rest of the process, so the resulting file is a complete,
    literal transcript of the run -- everything this script already prints,
    whatever the current -d/--debug verbosity is, plus (unlike the console)
    any sys.exit(...) error message and any genuinely unexpected traceback,
    which normally only reach stderr after this function would otherwise
    have returned. No new "what's worth logging" judgment calls needed: the
    existing print() calls throughout this script already are that
    judgment call -- e.g. read_dump_confirmed's per-attempt disagreements
    and print_failure_diagnostics' reports are unconditional (no -d
    needed), while the raw per-message SysEx trace stays behind -d as
    today, so a plain --log-file run captures the former without the
    latter, and --log-file -d together captures everything.

    A file open at the time of a crash needs to survive interpreter
    shutdown intact, which is less trivial than it looks: an atexit hook
    that closes the file WITHOUT first putting the original stdout/stderr
    back risks a second, unrelated failure -- the interpreter's own final
    "flush stdout/stderr" step (which always runs, after atexit) would then
    call .flush() on a _Tee whose file half is already closed, raising
    inside interpreter teardown where it can't be handled normally
    (reproduced directly: this showed up as an 'unraisable exception' and
    the wrong exit code, 120 instead of 1, until the restore-then-close
    order below was fixed).

    Deliberately not restoring stdout/stderr any earlier than that (e.g. via
    try/finally around just the command dispatch in main()): the whole
    point is capturing a sys.exit(...) message or an uncaught traceback,
    both of which are printed further up the call stack, after main()'s own
    frame -- a finally there would restore the real streams before that
    printing happens and miss it entirely.

    `path_arg` is a required argparse value (see --log-file's help for why
    it's NOT nargs='?' with an auto-generated default) so it's always a
    real, non-empty string in normal use; the auto-named fallback below is
    just a harmless safety net, not the primary way to get a filename."""
    path = Path(path_arg) if path_arg else Path(f"gp200_{datetime.now():%Y%m%d_%H%M%S}.log")
    f = open(path, "w", encoding="utf-8")
    f.write(f"gp200.py run log -- version {SCRIPT_VERSION}, started "
             f"{datetime.now().isoformat(timespec='seconds')}\n")
    f.write(f"command: {' '.join(sys.argv)}\n")
    orig_stdout, orig_stderr = sys.stdout, sys.stderr

    def _footer():
        f.write(f"\n{'-' * 70}\nrun ended {datetime.now().isoformat(timespec='seconds')}\n")
        sys.stdout, sys.stderr = orig_stdout, orig_stderr  # see docstring: order matters
        f.close()

    sys.stdout = _Tee(orig_stdout, f)
    sys.stderr = _Tee(orig_stderr, f)
    atexit.register(_footer)
    print("Environment:")
    _print_environment_info()
    try:
        print(f"  MIDI inputs:     {', '.join(mido.get_input_names()) or '(none seen)'}")
        print(f"  MIDI outputs:    {', '.join(mido.get_output_names()) or '(none seen)'}")
    except Exception as e:
        print(f"  (couldn't enumerate MIDI ports up front: {e})")
    print("-" * 70)
    return path


def _save_failed_readback(slot: int, roundtrip_bytes) -> None:
    """Saves a failed verification's actual readback to disk unprompted, so
    it doesn't have to be manually re-exported to send along for diffing --
    see the established pattern this session of always comparing both the
    source and result files byte-for-byte."""
    if not roundtrip_bytes:
        return
    out_name = f"failed_verify_{slot_to_label(slot)}_{int(time.time())}.prst"
    try:
        Path(out_name).write_bytes(roundtrip_bytes)
        print(f"  saved the failed readback to {out_name} for later comparison")
    except OSError as e:
        print(f"  (couldn't save the failed readback to disk: {e})")


def print_failure_diagnostics(slot: int, mismatches, actual_name, method: str, commit: bool,
                               attempts: int, roundtrip_bytes):
    """Printed unconditionally -- with or without -d/--debug -- whenever a
    write never fully verifies after MAX_WRITE_ATTEMPTS, i.e. the point where
    the automatic retry safety net has already given up and a human needs to
    look at it. Bundles everything useful for troubleshooting later without
    having to reproduce the failure live on the spot: every mismatched field
    (not just the first few -- the per-retry log elsewhere truncates for
    readability, a final failure shouldn't), the environment fingerprint (see
    _print_environment_info), and the actual failed readback saved to disk
    (see _save_failed_readback)."""
    print("\n" + "=" * 70)
    print("WRITE FAILED TO VERIFY -- diagnostic info for troubleshooting:")
    print("=" * 70)
    print(f"  time:            {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  slot:            {slot_to_label(slot)} (slot {slot})")
    print(f"  method:          {method}{' (with save-commit)' if commit and method == 'flash' else ''}")
    print(f"  attempts used:   {attempts}")
    print(f"  device now reads: {actual_name!r}")
    _print_environment_info()
    if mismatches:
        print(f"  {len(mismatches)} mismatched field(s), full list:")
        print(_format_mismatches(mismatches, limit=len(mismatches)))
    else:
        print(f"  couldn't verify by reading the slot back on the final attempt: {actual_name}")
    _save_failed_readback(slot, roundtrip_bytes)
    print("=" * 70)


def cmd_upload(args):
    """Writes one or more .prst files -- or a single .zip of them -- starting
    at a destination slot. Merges what used to be two separate commands
    (`upload`, for exactly one file, and `import`, for several files via a
    `--start` flag) into one, since a single file was always just the N=1
    case of the same operation (2026-09-27). The destination is always the
    LAST positional argument, e.g. `upload patch.prst 34-B` or `upload
    a.prst b.prst c.prst 10-A` or `upload backup.zip 10-A` -- never a flag,
    and never inferred from a zip's own contents (see expand_import_sources
    for why: a filename's embedded slot label, when present, only controls
    import ORDER, not destination)."""
    if len(args.targets) < 2:
        sys.exit("upload needs at least one file (or a .zip) AND a destination slot, "
                  "e.g.: upload patch.prst 34-B")
    *file_args, slot_arg = args.targets
    start = label_to_slot(slot_arg)  # validate before opening the connection
    sources = expand_import_sources(file_args)  # plain .prst list, or one .zip unpacked
    skeleton = _resolve_verify_skeleton()
    dev = Device(args.port, debug=args.debug)
    failures = []
    try:
        for i, (fname, file_bytes) in enumerate(sources):
            slot = start + i
            if slot >= TOTAL_SLOTS:
                print(f"  stopping: ran out of slots after {slot_to_label(slot - 1)}")
                break
            if len(file_bytes) != PRST_LEN or file_bytes[0:4] != b"TSRP":
                print(f"  skipping {fname}: not a valid .prst file")
                continue
            print(f"{fname} -> {slot_to_label(slot)}")
            if not confirm_overwrite(dev, slot, args.force):
                print("  skipped")
                continue
            ok, mismatches, actual, attempts, roundtrip = write_and_verify(
                dev, slot, file_bytes, args.method, args.commit, skeleton)
            if ok:
                attempt_note = "" if attempts == 1 else f", took {attempts} attempts"
                print(f"  verified byte-for-byte{attempt_note}: now reads {actual!r}")
            else:
                print_failure_diagnostics(slot, mismatches, actual, args.method, args.commit,
                                           attempts, roundtrip)
                failures.append(slot_to_label(slot))
            # A little breathing room before hammering the device with the
            # next slot's write burst -- cheap insurance for long bulk runs,
            # on top of write_and_verify's own retry logic.
            time.sleep(0.3)
    finally:
        dev.close()
    if failures:
        sys.exit(f"{len(failures)} slot(s) failed verification: {', '.join(failures)}")


def cmd_apply_template(args):
    file_bytes = Path(args.file).read_bytes()
    if len(file_bytes) != PRST_LEN or file_bytes[0:4] != b"TSRP":
        sys.exit(f"{args.file} is not a valid .prst file")
    skeleton = _resolve_verify_skeleton()
    slots = parse_slot_range(args.start, args.end)  # validate before opening the connection
    dev = Device(args.port, debug=args.debug)
    failures = []
    try:
        for slot in slots:
            label = slot_to_label(slot)
            print(f"{label}")
            if not confirm_overwrite(dev, slot, args.force):
                print("  skipped")
                continue
            ok, mismatches, actual, attempts, roundtrip = write_and_verify(
                dev, slot, file_bytes, args.method, args.commit, skeleton)
            if ok:
                attempt_note = "" if attempts == 1 else f", took {attempts} attempts"
                print(f"  verified byte-for-byte{attempt_note}: now reads {actual!r}")
            else:
                print_failure_diagnostics(slot, mismatches, actual, args.method, args.commit,
                                           attempts, roundtrip)
                failures.append(label)
            time.sleep(0.3)
    finally:
        dev.close()
    if failures:
        sys.exit(f"{len(failures)} slot(s) failed verification: {', '.join(failures)}")


def cmd_diag_write(args):
    """The high-slot addressing test: write to `slot`, then read back both
    `slot` and the slot the upload's 7-bit mask would alias it to, after
    backing up whatever is currently in the aliased slot."""
    slot = label_to_slot(args.slot)  # validate everything before opening the connection
    aliased = slot & 0x7F
    if aliased == slot:
        sys.exit(f"{args.slot} is slot {slot}, already below 128; "
                  "this test is only meaningful for banks 33+.")
    try:
        skeleton = resolve_skeleton_bytes(args.skeleton)
    except (FileNotFoundError, ValueError) as e:
        sys.exit(f"Couldn't load a skeleton .prst ({e})")
    template_path = Path(args.template)
    file_bytes = template_path.read_bytes()
    if len(file_bytes) != PRST_LEN or file_bytes[0:4] != b"TSRP":
        sys.exit(f"{template_path} is not a valid .prst file")

    dev = Device(args.port, debug=args.debug)
    try:
        print(f"Target: {args.slot} (slot {slot}).  If the 7-bit mask bites, "
              f"it will land on {slot_to_label(aliased)} (slot {aliased}) instead.")

        print(f"Backing up {slot_to_label(aliased)} first, in case the write lands there...")
        # read_dump_confirmed rather than a bare read_dump: this backup is the
        # only copy of whatever's currently in the aliased slot, taken right
        # before a write that might overwrite it -- the same "accurate backup"
        # reasoning as cmd_export applies here, maybe more so given there's no
        # do-over if this one silently saves a read-glitched byte. If even a
        # confirmed read can't be gotten, better to stop here than proceed
        # with a write that has no reliable safety net behind it.
        try:
            backup = build_prst_from_dump(dev.read_dump_confirmed(aliased), "backup", skeleton,
                                           debug=args.debug)
        except TimeoutError as e:
            sys.exit(f"Couldn't back up {slot_to_label(aliased)} before writing ({e}) -- "
                      "aborting rather than writing without a safety copy.")
        Path(args.backup_out).write_bytes(backup)
        print(f"  saved to {args.backup_out}")

        print(f"Writing {args.template} to {args.slot}...")
        dev.write_slot(slot, file_bytes, None)

        name_target = dev.read_name(slot)
        name_aliased = dev.read_name(aliased)
        print(f"\n{args.slot} now reads: {name_target!r}")
        print(f"{slot_to_label(aliased)} now reads: {name_aliased!r}")
        print("\nIf the target changed to match your uploaded file: high-slot writes work correctly.")
        print(f"If {slot_to_label(aliased)} changed instead: the 7-bit truncation is real; "
              f"its original contents are saved in {args.backup_out}.")
    finally:
        dev.close()


def cmd_calibrate_settle(args):
    """Stress test proposed by the user to empirically pin down whether the
    occasional write_and_verify retries are caused by reading the device back
    before it has actually finished committing the flash write ("settle
    time"), as opposed to something else (cable/interface flakiness, host
    timing jitter, etc). Repeatedly writes the same .prst to the same slot
    and fully verifies it, bypassing write_and_verify's own retry-then-
    give-up logic entirely -- every attempt at every delay is reported, since
    the point here is measuring *how often* attempts fail at a given delay,
    not just catching failures. Any mismatch resets the consecutive-success
    streak and bumps the settle delay by --step; once --target-streak clean
    writes happen in a row, that delay is reported as stable. If the delay
    theory is right, streaks should get reliably longer as the delay grows.
    If failures keep happening even near --max-settle, that theory doesn't
    fully explain it and something else is going on.

    Every mismatched file offset seen along the way is tallied (not just the
    most recent one), and printed as a summary -- sorted most-frequent-first
    -- whenever the run ends, including on Ctrl-C or a safety-cap exit, not
    only on a clean finish. A real run against actual hardware showed every
    single mismatch, across many attempts and several different settle
    delays, landing on the exact same file offset every time: strong evidence
    that isn't 'random transient corruption' in the abstract, but a specific,
    reproducible weak point, which longer settle delays alone did not
    reliably fix. This tally is what makes a pattern like that visible instead
    of scrolling past in the per-attempt log."""
    slot = label_to_slot(args.slot)  # validate before opening the connection
    file_bytes = Path(args.file).read_bytes()
    if len(file_bytes) != PRST_LEN or file_bytes[0:4] != b"TSRP":
        sys.exit(f"{args.file} is not a valid .prst file")
    skeleton = _resolve_verify_skeleton()
    dev = Device(args.port, debug=args.debug)
    settle = args.start_settle
    streak = 0
    total_attempts = 0
    total_failures = 0
    mismatch_tally = {}  # file offset -> number of times it has ever mismatched
    last_roundtrip = None  # most recent failed readback, for _save_failed_readback on give-up

    def print_summary(stable: bool):
        if stable:
            print(f"\nStable at settle={settle:.2f}s: {args.target_streak} consecutive clean "
                  f"write(s), out of {total_attempts} attempt(s) total "
                  f"({total_failures} mismatch(es) along the way).")
        else:
            print(f"\nStopped after {total_attempts} attempt(s), last settle delay {settle:.2f}s "
                  f"({total_failures} mismatch(es) seen, streak was {streak}/{args.target_streak}).")
        if mismatch_tally:
            print("Offsets that ever mismatched, most frequent first:")
            for off, n in sorted(mismatch_tally.items(), key=lambda kv: -kv[1]):
                print(f"    {describe_prst_offset(off)} (0x{off:04X}): {n} time(s)")
            if len(mismatch_tally) == 1:
                print("  -- always the SAME offset. That points to a specific, reproducible weak "
                      "point (in the transfer or the device's handling of it), not random noise; "
                      "a settle-delay bump that doesn't stop it recurring means the delay isn't "
                      "the (whole) cause.")
        elif total_failures == 0:
            print("No mismatches at all, even at the starting delay -- if ordinary uploads have "
                  "been showing retries, this run may just have gotten lucky; consider a lower "
                  "--start-settle or a longer --target-streak to get a more sensitive read.")
        if not stable:
            # Only on a genuine give-up (safety cap or Ctrl-C), not a clean
            # finish -- same "diagnostics unconditionally on failure" policy
            # as write_and_verify's callers (see print_failure_diagnostics).
            _print_environment_info()
            _save_failed_readback(slot, last_roundtrip)

    try:
        if not confirm_overwrite(dev, slot, args.force):
            print("  skipped")
            return
        print(f"Calibrating settle delay on {args.slot}: starting at {settle:.2f}s, "
              f"stepping by {args.step:.2f}s on each mismatch, target streak "
              f"{args.target_streak} clean write(s) in a row "
              f"(caps: {args.max_settle:.2f}s delay / {args.max_attempts} attempts).\n")
        try:
            while True:
                total_attempts += 1
                if total_attempts > args.max_attempts:
                    print_summary(stable=False)
                    sys.exit(f"Error: gave up after {args.max_attempts} attempts without reaching "
                             f"a {args.target_streak}-streak (last settle delay tried: "
                             f"{settle:.2f}s). The settle-time theory alone may not fully explain "
                             "the retries.")
                dev.write_slot(slot, file_bytes, None, commit=args.commit, settle_s=settle)
                ok, mismatches, actual, roundtrip = dev.verify_write_full(slot, file_bytes, skeleton)
                if ok:
                    streak += 1
                    print(f"  [{total_attempts:3d}] settle={settle:.2f}s  OK    "
                          f"(streak {streak}/{args.target_streak})")
                    if streak >= args.target_streak:
                        break
                else:
                    total_failures += 1
                    streak = 0
                    last_roundtrip = roundtrip
                    for off, exp, act in (mismatches or []):
                        mismatch_tally[off] = mismatch_tally.get(off, 0) + 1
                    n = len(mismatches) if mismatches else "?"
                    detail = _format_mismatches(mismatches) if mismatches else f"    {actual}"
                    print(f"  [{total_attempts:3d}] settle={settle:.2f}s  MISMATCH ({n} field(s)) -- "
                          f"streak reset, bumping delay to {settle + args.step:.2f}s\n{detail}")
                    settle += args.step
                    if settle > args.max_settle:
                        print_summary(stable=False)
                        sys.exit(f"Error: settle delay exceeded the cap ({args.max_settle:.2f}s) "
                                 f"without reaching a clean {args.target_streak}-streak. This "
                                 "suggests the settle-time theory doesn't fully explain the retries.")
                time.sleep(0.3)
        except KeyboardInterrupt:
            print_summary(stable=False)
            sys.exit("Interrupted by user.")
    finally:
        dev.close()

    print_summary(stable=True)


def cmd_reread(args):
    """Reads the same slot repeatedly with NO writes at all in between, to
    test an alternative to "the GP-200's flash write is occasionally flaky":
    that the mismatches write_and_verify/calibrate-settle catch are actually
    happening on the HOST side, in how python-rtmidi/the OS MIDI stack
    receives incoming SysEx, rather than in what the device actually has
    stored. This is a real, documented category of issue -- python-rtmidi's
    Windows backend uses a small number of fixed-size preallocated buffers
    for incoming SysEx via the Windows Multimedia API (see
    https://github.com/SpotlightKid/python-rtmidi/issues/200 and
    https://github.com/mido/mido/issues/207) -- so it's a real possibility,
    not a stretch. Since nothing is written between reads here, the device's
    own flash content cannot change; if two reads of it still disagree with
    each other, the disagreement can only be coming from the read/receive
    side, not the device's stored data. Compares every read against the
    first successful one (or against --against FILE, e.g. the file you
    believe is actually on the device, if you have it)."""
    slot = label_to_slot(args.slot)  # validate before opening the connection
    skeleton = _resolve_verify_skeleton()
    reference = None
    if args.against:
        reference = Path(args.against).read_bytes()
        if len(reference) != PRST_LEN or reference[0:4] != b"TSRP":
            sys.exit(f"{args.against} is not a valid .prst file")
    dev = Device(args.port, debug=args.debug)
    results = []
    try:
        print(f"Reading {args.slot} {args.count} time(s), no writes in between, "
              f"{args.delay:.2f}s apart" +
              (f", comparing each to {args.against}" if reference is not None
               else ", comparing each to the first read") + "...\n")
        for i in range(1, args.count + 1):
            try:
                # Intentionally the RAW read_dump, not read_dump_confirmed: this
                # command's whole purpose is to expose read-to-read disagreement
                # when nothing is being written. Confirming reads here would
                # silently paper over exactly the instability it exists to
                # surface, and this is precisely the test that discovered it.
                dump = dev.read_dump(slot)
                rebuilt = build_prst_from_dump(dump, "reread", skeleton, debug=args.debug)
            except TimeoutError:
                print(f"  [{i:2d}] (no response)")
                results.append(None)
                time.sleep(args.delay)
                continue
            results.append(rebuilt)
            base = reference if reference is not None else next(r for r in results if r is not None)
            mismatches = diff_prst_content(base, rebuilt)
            if not mismatches:
                print(f"  [{i:2d}] matches")
            else:
                print(f"  [{i:2d}] DIFFERS ({len(mismatches)} field(s)):")
                print(_format_mismatches(mismatches, limit=len(mismatches)))
            time.sleep(args.delay)
    finally:
        dev.close()

    good = [r for r in results if r is not None]
    print()
    if not good:
        print("No successful reads at all -- can't say anything about stability.")
        return
    base = reference if reference is not None else good[0]
    all_same = all(diff_prst_content(base, r) == [] for r in good)
    if all_same:
        print(f"All {len(good)} successful read(s) agree with each other"
              + (" and with the reference file" if reference is not None else "")
              + " -- no evidence of read-side instability in this run.")
    else:
        print(f"{len(good)} read(s) of the SAME, unwritten slot did NOT all agree with each "
              "other. Since nothing was written between reads, the device's own flash content "
              "cannot have changed -- so this points at instability on the host's receive side "
              "(python-rtmidi / the OS MIDI stack), not at the GP-200's write path.")


def cmd_raw_sweep(args):
    """Added 2026-09-27, prompted directly by reviewing GP200 Studio's own
    source: its pull-a-patch path does exactly ONE read per slot, retrying
    only if nothing comes back within the timeout (never on content) -- no
    comparison against a second read, ever. That's precisely this tool's own
    `read_dump` (RETRY_COUNT=1, i.e. one retry-on-timeout-only), as opposed
    to `read_dump_confirmed`'s up-to-5-reads-until-two-agree scheme.

    Studio's export producing the SAME 245/256 result twice doesn't by
    itself prove Studio's reads are clean -- finding 14 in PROTOCOL_NOTES.md
    already showed it has no way to tell a glitched read from a good one, so
    a glitch there is simply used as-is. What it leaves genuinely open is
    whether THIS tool's own transport, run with Studio's exact strategy
    (one read, retry only on timeout, no verification), would look as
    unstable as `read_dump_confirmed`'s retry counts imply, or whether the
    byte-level glitching this project has documented (the `reread` command,
    finding 5) is rare enough or confined to fields obscure enough that a
    single raw pass would usually look fine too.

    This command answers that directly and cheaply: for every slot in
    range, do ONE raw read_dump (the Studio-equivalent read) and, right
    after it, a read_dump_confirmed (this tool's own trusted ground truth),
    and compare them with the same raw_dumps_agree logic read_dump_confirmed
    itself uses. Run twice in a row (or against two different sessions) and
    compare which slots disagreed each time: if the set of raw-vs-confirmed
    mismatches is different between runs, that's independent read-side
    noise, consistent with everything else this project has found; if it's
    the same slots every time, that would point at something more
    systematic and would be a real surprise worth chasing further."""
    range_given = args.start is not None or args.end is not None
    if range_given and not (args.start and args.end):
        sys.exit("--start and --end must be given together")
    if args.all and range_given:
        sys.exit("give --all or --start/--end, not both")
    if range_given:
        slots = parse_slot_range(args.start, args.end)
    elif args.all:
        slots = list(range(TOTAL_SLOTS))
    else:
        slots = [label_to_slot(args.slot)] if args.slot else None
    if slots is None:
        sys.exit("raw-sweep needs a slot, or --all, or --start/--end")

    dev = Device(args.port, debug=args.debug)
    mismatches = []
    raw_failed = []
    confirm_failed = []
    try:
        for slot in slots:
            label = slot_to_label(slot)
            try:
                raw = dev.read_dump(slot)
            except TimeoutError:
                print(f"{label}: raw read timed out (even with read_dump's own retry)")
                raw_failed.append(label)
                continue
            try:
                confirmed = dev.read_dump_confirmed(slot)
            except ReadNotConfirmedError as e:
                print(f"{label}: couldn't get a confirmed reading to compare against ({e})")
                confirm_failed.append(label)
                continue
            if raw_dumps_agree(raw, confirmed):
                print(f"{label}: raw read matches confirmed reading")
            else:
                detail = _describe_raw_dump_diff(raw, confirmed)
                print(f"{label}: MISMATCH -- {detail}")
                mismatches.append(label)
    finally:
        dev.close()

    total = len(slots)
    print()
    print(f"{total} slot(s) checked: {total - len(mismatches) - len(raw_failed) - len(confirm_failed)} "
          f"clean, {len(mismatches)} where the raw (Studio-style) read disagreed with the confirmed "
          f"reading, {len(raw_failed)} raw read timeout(s), {len(confirm_failed)} slot(s) where even "
          "read_dump_confirmed couldn't settle on an answer.")
    if mismatches:
        print("Mismatched slots: " + ", ".join(mismatches))
    if raw_failed:
        print("Raw read timeouts: " + ", ".join(raw_failed))
    if confirm_failed:
        print("Unconfirmable slots: " + ", ".join(confirm_failed))
    print("\nRun this again (ideally right away) and compare the mismatched-slots list to this "
          "one: a different list each time points at independent host-side read noise (the "
          "existing theory); the same list every time would be a new finding worth writing up.")


def _confirm_discrepancy(dev, slot, skeleton, file_bytes, roundtrip, n_reads):
    """Called right after a soak cycle's write+verify reports a mismatch.
    Does n_reads MORE reads of the same slot -- no further writes -- to
    answer a question write_and_verify's retry loop and read_dump_confirmed
    can't, on their own: is this specific bad value actually STORED on the
    device (every extra read agrees with it), or was it itself read-side
    noise that happened to repeat on both of read_dump_confirmed's reads
    (extra reads now disagree with each other, or come back matching the
    source file instead)? Read and write failures are not mutually
    exclusive -- this exists to tell them apart rather than assume it must
    be one or the other."""
    print(f"    Confirming: {n_reads} more read(s) of {slot_to_label(slot)}, no further writes...")
    verdicts = []
    for i in range(1, n_reads + 1):
        try:
            dump = dev.read_dump(slot)
        except TimeoutError:
            print(f"      [{i}] (no response)")
            verdicts.append("no_response")
            time.sleep(0.2)
            continue
        confirm_rt = build_prst_from_dump(dump, "confirm", skeleton, debug=dev.debug)
        matches_bad = diff_prst_content(roundtrip, confirm_rt) == []
        matches_source = diff_prst_content(file_bytes, confirm_rt) == []
        if matches_source:
            print(f"      [{i}] matches the SOURCE file (correct)")
            verdicts.append("good")
        elif matches_bad:
            print(f"      [{i}] matches the bad value")
            verdicts.append("bad")
        else:
            print(f"      [{i}] differs from both the bad value and the source")
            verdicts.append("other")
        time.sleep(0.2)
    n_bad = verdicts.count("bad")
    n_good = verdicts.count("good")
    n_other = verdicts.count("other")
    counted = n_bad + n_good + n_other
    if counted == 0:
        print("    No successful confirmation reads -- can't say anything about this one.")
    elif n_bad == counted:
        print(f"    All {counted} confirmation read(s) agree with the bad value -- this specific "
              "discrepancy looks STORED on the device, not just a read glitch.")
    elif n_good == counted:
        print(f"    All {counted} confirmation read(s) now match the source file -- the original "
              "mismatch looks like read-side noise, not a stored discrepancy.")
    else:
        print(f"    Confirmation reads disagree with each other ({n_bad} bad / {n_good} good / "
              f"{n_other} other) -- the read side is still unstable enough that this one can't "
              "be pinned on the write path with confidence.")
    return verdicts


def cmd_soak(args):
    """Runs --count write+verify cycles of the same file to the same slot, all
    at a FIXED settle delay that is never adjusted based on the outcome (this
    is the key difference from calibrate-settle, which deliberately changes
    the delay on every failure -- exactly the thing you don't want when
    trying to isolate ONE variable, like the power supply, by holding
    everything else constant). A mismatch here is never treated as fatal;
    the whole point is just to measure a clean failure-RATE number (X/COUNT)
    for one fixed set of conditions, so it can be compared directly against
    another run where exactly one thing changed (a different power supply, a
    different cable, a different computer).

    A single run can't tell a genuinely worse condition apart from ordinary
    bad luck -- run this a few times per condition before comparing. --label
    is purely cosmetic, printed in the summary, so pasted-back output from
    several runs stays easy to tell apart.

    --confirm-reads N: read and write failures are not mutually exclusive --
    proving the read side is occasionally unreliable (see `reread`) says
    nothing about whether writes are ALSO occasionally unreliable. Each
    cycle's write+verify already uses read_dump_confirmed internally (two
    agreeing reads before a mismatch is even reported), which makes a pure
    read glitch an unlikely explanation for any failure this loop reports --
    but "unlikely" isn't "impossible" once you're comparing exported .prst
    files or running the same check dozens of times. So on any mismatch, this
    does N MORE no-write re-reads of the slot afterward and reports whether
    they agree with the bad value (stored -- a real write-side discrepancy),
    come back matching the source (the mismatch itself was transient noise),
    or disagree with each other (the read side is still too unstable to
    conclude anything from this one failure). --stop-on-failure halts the
    run right after that confirmation, instead of continuing for the full
    --count cycles, so a real hardware discrepancy can be inspected (or the
    device power-cycled and re-read) before anything else overwrites it."""
    slot = label_to_slot(args.slot)  # validate before opening the connection
    file_bytes = Path(args.file).read_bytes()
    if len(file_bytes) != PRST_LEN or file_bytes[0:4] != b"TSRP":
        sys.exit(f"{args.file} is not a valid .prst file")
    skeleton = _resolve_verify_skeleton()
    label_note = f"  [{args.label}]" if args.label else ""
    dev = Device(args.port, debug=args.debug)
    total_failures = 0
    cycles_run = 0
    mismatch_tally = {}  # file offset -> number of times it has ever mismatched
    try:
        if not confirm_overwrite(dev, slot, args.force):
            print("  skipped")
            return
        print(f"Soak test on {args.slot}: {args.count} write+verify cycle(s) at a fixed "
              f"settle={args.settle:.2f}s{label_note}")
        _print_environment_info()
        print()
        for i in range(1, args.count + 1):
            cycles_run = i
            dev.write_slot(slot, file_bytes, None, commit=args.commit, settle_s=args.settle)
            ok, mismatches, actual, roundtrip = dev.verify_write_full(slot, file_bytes, skeleton)
            if ok:
                print(f"  [{i:3d}/{args.count}] OK")
            else:
                total_failures += 1
                for off, exp, act in (mismatches or []):
                    mismatch_tally[off] = mismatch_tally.get(off, 0) + 1
                n = len(mismatches) if mismatches else "?"
                detail = _format_mismatches(mismatches) if mismatches else f"    {actual}"
                print(f"  [{i:3d}/{args.count}] MISMATCH ({n} field(s))\n{detail}")
                if args.confirm_reads > 0 and roundtrip is not None:
                    _confirm_discrepancy(dev, slot, skeleton, file_bytes, roundtrip, args.confirm_reads)
                if args.stop_on_failure:
                    print(f"\n--stop-on-failure: stopping after cycle {i}/{args.count}.")
                    break
            time.sleep(0.3)
    finally:
        dev.close()

    # cycles_run, not args.count: --stop-on-failure can end the run early, and
    # reporting "1/20" for a run that only actually attempted 3 cycles (as a
    # real hardware run did on 2026-09-27) is a wrong rate, not just a
    # cosmetic quirk -- 1/3 (33%) and 1/20 (5%) tell very different stories.
    rate = (total_failures / cycles_run * 100) if cycles_run else 0.0
    stopped_early_note = f" ({cycles_run} attempted before stopping)" if cycles_run < args.count else ""
    print(f"\nResult{label_note}: {total_failures}/{cycles_run} failed ({rate:.0f}%){stopped_early_note}")
    if mismatch_tally:
        print("Offsets that ever mismatched, most frequent first:")
        for off, n in sorted(mismatch_tally.items(), key=lambda kv: -kv[1]):
            print(f"    {describe_prst_offset(off)} (0x{off:04X}): {n} time(s)")

# ----------------------------------------------------------------- main ----

def _prog_name() -> str:
    """The name to show in usage examples -- whatever this actually is (a
    .py, gp200.exe, gp200-linux, gp200-macos), not a hardcoded guess. People
    running the standalone executable via a downloaded README have no
    Python in the picture at all, so a hardcoded 'gp200.py' in every example
    would be actively wrong for them."""
    return Path(sys.argv[0]).name


CLI_EPILOG_TEMPLATE = """\
Quick examples:
  {prog} list-ports              # see if this can find your GP-200
  {prog} list                    # print every patch name (256 slots)
  {prog} export --all            # back up every patch to a .zip file
  {prog} upload backup.zip 1-A   # fills slots starting at 1-A, in order

A zip/multi-file upload always fills CONSECUTIVE slots starting at the one
you give -- to restore a backup to where it came from, upload it to the
SAME slot you exported it from (see 'upload --help' for the details,
including what happens if the export skipped a slot).

This has only been tested against one GP-200 -- back up your patches
(export --all) before uploading or overwriting anything.

Full details, protocol notes, and source:
  https://github.com/donpark2000/GP-200-Patch-Manager
"""

NO_ARGS_MESSAGE_TEMPLATE = """\
GP-200 Patch Manager
=====================
This is a command-line tool -- it runs inside a terminal window, not by
double-clicking the icon. To use it:

  1. Open a terminal (on Windows: search "cmd" or "PowerShell" in the
     Start menu; on Linux/macOS: your usual Terminal app).
  2. Go to the folder this program is in, e.g.:
       cd Downloads
  3. Try one of these:
       {prog} list-ports              (see if it finds your GP-200)
       {prog} list                    (see every patch name on the device)
       {prog} export --all            (back up every patch to a .zip file)
       {prog} upload backup.zip 1-A   (fills slots starting at 1-A, in order)

This has only been tested against one GP-200 -- back up your patches
(export --all) before uploading or overwriting anything. A zip/multi-file
upload always fills consecutive slots starting where you tell it, so to
restore a backup, upload it back to the SAME slot you exported it from
(run "{prog} upload --help" for the details).

For every command and option: {prog} --help
"""


def _should_pause_before_exit() -> bool:
    """Whether to wait for a keypress after the no-args message, before
    exiting -- true for any frozen (PyInstaller) build, never for a
    source-code `python gp200.py` run (nothing to protect there: the
    terminal that ran it isn't going anywhere regardless).

    This used to also try to detect "was this actually a double-click, in a
    console Windows just created" via GetConsoleProcessList, and skip the
    pause otherwise -- specifically to avoid a "press Enter to close this
    window" prompt that makes no sense when run from an already-open
    terminal (real complaint, 2026-09-28). But real-hardware testing the
    same day found that detection unreliable: it's meant to tell a
    double-click apart from an existing shell by counting processes
    attached to the console, but that count can't tell a shell apart from
    a terminal HOST process (e.g. Windows Terminal's own backend) that
    Windows may attach to a freshly-opened console too -- undetectable
    from here without more real Windows environments to test against than
    this project has access to. Rather than ship a second unverified guess
    on top of the first, this just always pauses when frozen, and the
    wording below is written to make sense either way instead of trying to
    be clever about when to show it."""
    return getattr(sys, "frozen", False)


def _flush_stray_windows_keystrokes():
    """Discards anything already sitting in the console's keyboard input
    buffer, via FlushConsoleInputBuffer. Real-hardware testing (2026-09-28)
    found the pause below could be satisfied instantly -- window still
    flashing shut before the message could be read, even though
    _should_pause_before_exit() correctly said to pause. The likely cause:
    dismissing the "Windows protected your PC" SmartScreen dialog with the
    Enter key (its default action) can leave that same keystroke sitting in
    the brand-new console's input buffer, which input() then reads
    immediately -- consuming the pause before the user ever gets to press
    their OWN key. A short pause first lets any such trailing key-up/key-down
    events actually arrive before they're discarded; without it, a flush can
    race the still-arriving keystroke and miss it.

    Windows-only API (FlushConsoleInputBuffer doesn't exist elsewhere) --
    a no-op on the Linux/macOS builds, which don't have this SmartScreen-style
    dialog-dismissal path to begin with."""
    if sys.platform != "win32":
        return
    import ctypes, time
    time.sleep(0.3)
    STD_INPUT_HANDLE = -10
    handle = ctypes.windll.kernel32.GetStdHandle(STD_INPUT_HANDLE)
    ctypes.windll.kernel32.FlushConsoleInputBuffer(handle)


def main():
    # A musician downloading the standalone executable is likely to just
    # double-click it, the way any other Windows program is launched. A
    # console app with no arguments and argparse's default `required=True`
    # subparser error exits immediately with a usage message -- which, for
    # a double-clicked .exe, means a window flashes open and closes before
    # anyone can read it. Handle this case explicitly with a plain-language
    # message instead of the technical argparse error, and (for any frozen
    # build -- see _should_pause_before_exit()) wait for a keypress
    # afterward. The prompt says "continue", not "close this window": it's
    # equally true whether this just-created console is about to close (the
    # double-click case) or this is already sitting in an open terminal
    # (where "continue" just means "return to your prompt") -- deliberately
    # not trying to tell those two cases apart, see that function's
    # docstring for why.
    if len(sys.argv) == 1:
        print(NO_ARGS_MESSAGE_TEMPLATE.format(prog=_prog_name()))
        if _should_pause_before_exit():
            try:
                _flush_stray_windows_keystrokes()
            except Exception:
                # Best-effort -- if this can't be done for some reason,
                # still fall through to the plain pause below rather than
                # skipping it entirely.
                pass
            input("Press Enter to continue...")
        sys.exit(0)

    p = argparse.ArgumentParser(
        description="Command-line patch manager for the Valeton GP-200.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=CLI_EPILOG_TEMPLATE.format(prog=_prog_name()))
    p.add_argument("--port", help="substring of the MIDI port name, if auto-detect finds none or too many")
    p.add_argument("-d", "--debug", action="store_true",
                    help="print every SysEx message sent and received, including ones that don't match "
                         "what the command expected")
    p.add_argument("--log-file", metavar="PATH", default=None,
                    help="save a full copy of this run's output to PATH, including environment info "
                         "(Python/mido/rtmidi versions, OS, MIDI ports seen) and any error -- so the "
                         "file can be sent along if something goes wrong. Combine with -d for the full "
                         "SysEx trace as well. (A required value, not nargs='?' -- with a subcommand "
                         "positional right after it on the command line, an optional value here would "
                         "have swallowed the subcommand name instead: `--log-file list-ports` would set "
                         "log_file='list-ports' and leave no command at all, not run list-ports with "
                         "logging on. Found this the hard way while testing, before it shipped.)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("list-ports", help="show every MIDI port Python can see")
    s.set_defaults(func=cmd_list_ports)

    s = sub.add_parser("list", help="print the name in every one of the 256 slots")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("read", help="print one slot's name (fast, no writing)")
    s.add_argument("slot", help="e.g. 34-B")
    s.set_defaults(func=cmd_read)

    s = sub.add_parser("export", help="save one slot, a range, or every slot, to .prst file(s)")
    s.add_argument("slot", nargs="?", help="e.g. 34-B (omit with --all or --start/--end)")
    s.add_argument("-o", "--out", help="output .prst file, or .zip with --all/--start+--end "
                                        "(default: named after the slot and patch name, or "
                                        "after the range/'all_patches' for a zip)")
    s.add_argument("--all", action="store_true", help="export all 256 slots into a zip")
    s.add_argument("--start", help="export a range: first slot, e.g. 34-A (use with --end)")
    s.add_argument("--end", help="export a range: last slot, e.g. 36-D (use with --start)")
    s.add_argument("--skeleton", help=f"template .prst to reconstruct exports from (default: {DEFAULT_SKELETON.name})")
    s.add_argument("--force", action="store_true", help="skip the confirmation if the output file already exists")
    s.set_defaults(func=cmd_export)

    write_method_help = ("'flash' (default) sends the whole file as raw flash-write chunks -- "
                          "fast, but neither this script nor GP200 Studio has ever fully "
                          "trusted it to stick. 'live' decodes the file and replays it as "
                          "individual parameter edits (the same messages turning knobs on the "
                          "device would send) followed by a save-commit -- slower, but the one "
                          "path GP200 Studio has trusted since its first commit.")

    s = sub.add_parser("upload", help="write one or more .prst files (or one .zip of them) to a slot")
    s.add_argument("targets", nargs="+", metavar="FILE",
                    help="one or more .prst files, or a single .zip, followed by the "
                         "DESTINATION SLOT as the last argument -- e.g. 'patch.prst 34-B', "
                         "'a.prst b.prst c.prst 10-A', or 'backup.zip 10-A'. Multiple "
                         "files/a zip ALWAYS write to consecutive slots starting there -- "
                         "e.g. a 10-entry zip uploaded to 10-A fills 10-A, 10-B, 10-C... "
                         "This is NOT the same as 'restore each patch to the slot it came "
                         "from': in a zip, entries named like this tool's own export output "
                         "(e.g. 37A_Template.prst) are ordered by that slot label, but ONLY "
                         "to decide the order they're written in -- the destination is "
                         "always the last argument here, never read back out of the zip. "
                         "To actually restore a backup to where it came from, upload it to "
                         "the SAME starting slot you exported it from, e.g. a zip made with "
                         "'export --all' (which starts at 1-A) goes back with "
                         "'upload backup.zip 1-A'. And if that export skipped any slots "
                         "(printed as 'skipped' at the time), the zip has a gap that "
                         "ISN'T preserved -- everything after the gap will land one slot "
                         "earlier than it actually came from. Re-run export first to fill "
                         "any gaps before trusting a zip for a full restore.")
    s.add_argument("--force", action="store_true", help="skip the overwrite confirmation")
    s.add_argument("--method", choices=["flash", "live"], default="flash", help=write_method_help)
    s.add_argument("--commit", action="store_true",
                    help="--method flash only: send an experimental save-commit finalize after "
                         "the chunk burst. Unproven -- see build_save_commit's docstring for the risk.")
    s.set_defaults(func=cmd_upload)

    s = sub.add_parser("apply-template", help="write ONE .prst file into every slot in a range")
    s.add_argument("file")
    s.add_argument("start", help="e.g. 30-A")
    s.add_argument("end", help="e.g. 34-D")
    s.add_argument("--force", action="store_true")
    s.add_argument("--method", choices=["flash", "live"], default="flash", help=write_method_help)
    s.add_argument("--commit", action="store_true")
    s.set_defaults(func=cmd_apply_template)

    s = sub.add_parser("diag-write", help="the high-slot (bank 33+) addressing test, with an automatic backup")
    s.add_argument("slot", help="a slot in bank 33 or higher, e.g. 34-B")
    s.add_argument("--template", required=True, help=".prst to upload as the test payload")
    s.add_argument("--skeleton", help=f"template .prst for the backup (default: {DEFAULT_SKELETON.name}, "
                                        "or one embedded in this script if that's missing)")
    s.add_argument("--backup-out", default="diag_backup.prst")
    s.set_defaults(func=cmd_diag_write)

    s = sub.add_parser("calibrate-settle",
                        help="stress test: repeatedly write+verify the same file to find the "
                             "settle delay that eliminates verification retries")
    s.add_argument("slot", help="a slot to repeatedly overwrite for the test, e.g. 37-A")
    s.add_argument("file", help=".prst file to write repeatedly")
    s.add_argument("--force", action="store_true", help="skip the initial overwrite confirmation")
    s.add_argument("--commit", action="store_true",
                    help="include the experimental save-commit finalize on every write")
    s.add_argument("--start-settle", type=float, default=1.0,
                    help="initial settle delay in seconds (default: 1.0, matching normal uploads)")
    s.add_argument("--step", type=float, default=0.1,
                    help="seconds added to the settle delay after each mismatch (default: 0.1)")
    s.add_argument("--target-streak", type=int, default=20,
                    help="consecutive clean writes required to call a delay stable (default: 20)")
    s.add_argument("--max-settle", type=float, default=5.0,
                    help="safety cap on the settle delay in seconds (default: 5.0)")
    s.add_argument("--max-attempts", type=int, default=200,
                    help="safety cap on total attempts across the whole run (default: 200)")
    s.set_defaults(func=cmd_calibrate_settle)

    s = sub.add_parser("reread",
                        help="read the same slot repeatedly with NO writes, to test whether "
                             "mismatches are read-side (host/rtmidi) rather than write-side")
    s.add_argument("slot", help="a slot to repeatedly read, e.g. 37-A")
    s.add_argument("--count", type=int, default=10, help="number of reads (default: 10)")
    s.add_argument("--delay", type=float, default=0.2,
                    help="seconds between reads (default: 0.2)")
    s.add_argument("--against", help="compare every read against this .prst file instead of "
                                       "against the first successful read")
    s.set_defaults(func=cmd_reread)

    s = sub.add_parser("raw-sweep",
                        help="compare one raw (Studio-style, single-read) pass against "
                             "read_dump_confirmed across many slots, to see how often the two "
                             "strategies would actually disagree")
    s.add_argument("slot", nargs="?", help="a single slot to check, e.g. 37-A (omit with --all "
                                             "or --start/--end)")
    s.add_argument("--all", action="store_true", help="check all 256 slots")
    s.add_argument("--start", help="check a range: first slot, e.g. 34-A (use with --end)")
    s.add_argument("--end", help="check a range: last slot, e.g. 36-D (use with --start)")
    s.set_defaults(func=cmd_raw_sweep)

    s = sub.add_parser("soak",
                        help="run N write+verify cycles at a FIXED delay and report the failure "
                             "rate -- for comparing one changed condition (power supply, cable, "
                             "computer) against another")
    s.add_argument("slot", help="a slot to repeatedly overwrite for the test, e.g. 37-A")
    s.add_argument("file", help=".prst file to write repeatedly")
    s.add_argument("--count", type=int, default=20, help="number of write+verify cycles (default: 20)")
    s.add_argument("--settle", type=float, default=1.0,
                    help="fixed settle delay in seconds, never adjusted (default: 1.0)")
    s.add_argument("--force", action="store_true", help="skip the initial overwrite confirmation")
    s.add_argument("--commit", action="store_true",
                    help="include the experimental save-commit finalize on every write")
    s.add_argument("--label", help="free-text label included in the output, to keep multiple "
                                     "runs straight (e.g. 'stock supply, run 1')")
    s.add_argument("--confirm-reads", type=int, default=0,
                    help="on any mismatch, do this many EXTRA no-write re-reads afterward to "
                         "check whether the bad value is actually stored on the device or was "
                         "itself read-side noise (0 disables this; default: 0)")
    s.add_argument("--stop-on-failure", action="store_true",
                    help="stop the run as soon as a mismatch is found (after any --confirm-reads "
                         "check), instead of continuing for the full --count cycles")
    s.set_defaults(func=cmd_soak)

    args = p.parse_args()
    if args.log_file is not None:
        log_path = _start_run_log(args.log_file)
        print(f"(saving a full copy of this run to {log_path})")
    try:
        args.func(args)
    except (ValueError, FileNotFoundError, TimeoutError) as e:
        sys.exit(f"Error: {e}")


if __name__ == "__main__":
    main()
