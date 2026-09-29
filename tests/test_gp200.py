import importlib.util, random, sys
from pathlib import Path

GP200_PATH = str(Path(__file__).resolve().parent.parent / "gp200.py")
spec = importlib.util.spec_from_file_location("gp200", GP200_PATH)
gp200 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gp200)

failures = []

def check(name, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}")
    if not cond:
        failures.append(name)

# ---------------------------------------------------------------------------
# 1. slot <-> label round trip (banks 1-64, letters A-D), including the
#    34-B example and the 133 -> "2B" alias case that was corrected earlier.
# ---------------------------------------------------------------------------
ok = True
for slot in range(256):
    label = gp200.slot_to_label(slot)
    back = gp200.label_to_slot(label)
    if back != slot:
        ok = False
        print(f"  mismatch: slot {slot} -> {label} -> {back}")
check("slot_to_label/label_to_slot round trip over all 256 slots", ok)

check("label_to_slot('34-B') == 133", gp200.label_to_slot("34-B") == 133)
check("slot_to_label(133) == '34B'", gp200.slot_to_label(133) == "34B")
check("slot 133 & 0x7F == 5 aliases to slot_to_label(5) == '2B' (not '2A')",
      gp200.slot_to_label(133 & 0x7F) == "2B")

# ---------------------------------------------------------------------------
# 2. nibble encode/decode round trip
# ---------------------------------------------------------------------------
random.seed(42)
sample = bytes(random.randrange(256) for _ in range(1000))
enc = gp200.nibble_encode(sample)
dec = gp200.nibble_decode(enc)
check("nibble_encode/nibble_decode round trip (1000 random bytes)", dec == sample)
check("nibble_encode output is all 7-bit (<=0x7F)", all(b <= 0x7F for b in enc))

# ---------------------------------------------------------------------------
# 3. build_read_request / build_preset_change byte layout sanity
# ---------------------------------------------------------------------------
req = gp200.build_read_request(133)
check("build_read_request starts with HEADER", req[:8] == gp200.HEADER)
check("build_read_request ends with 0xF7", req[-1] == 0xF7)
check("build_read_request cmd/sub = 0x11/0x10 for full dump", req[8] == 0x11 and req[9] == 0x10)

# ---------------------------------------------------------------------------
# 4. THE FIX: offset_of / assemble_chunks 7-bit split decoding
#
# These are the real chunk-header (msg[11], msg[12]) byte pairs captured
# from the user's actual `export 36-C` debug run (7 chunks of a full dump).
# ---------------------------------------------------------------------------
real_pairs = [
    (0x00, 0x00),
    (0x39, 0x01),
    (0x72, 0x02),
    (0x2b, 0x04),
    (0x64, 0x05),
    (0x1d, 0x07),
    (0x56, 0x08),
]
expected_offsets = [0, 185, 370, 555, 740, 925, 1110]

# Build synthetic full sysex messages with correct layout:
# [0:8]=HEADER [8]=cmd [9]=sub [10]=slot [11]=off_lo7 [12]=off_hi7 [13:-1]=nibble payload [-1]=0xF7
def make_chunk(b11, b12, payload_len=185, seed=0):
    rnd = random.Random(seed)
    payload = bytes(rnd.randrange(0x80) for _ in range(payload_len))  # nibble data is 7-bit
    msg = bytearray()
    msg += gp200.HEADER
    msg.append(0x12)
    msg.append(0x18)
    msg.append(0x00)      # slot byte, irrelevant here
    msg.append(b11)
    msg.append(b12)
    msg += payload
    msg.append(0xF7)
    return bytes(msg), payload

chunks = []
payload_by_pair = {}
for i, (b11, b12) in enumerate(real_pairs):
    msg, payload = make_chunk(b11, b12, seed=i)
    chunks.append(msg)
    payload_by_pair[(b11, b12)] = payload

# Access the private offset_of the same way assemble_chunks computes it, by
# re-deriving it from the fixed source (can't import a nested function
# directly, so recompute using the same formula under test).
def offset_of_fixed(msg):
    return (msg[11] & 0x7F) | ((msg[12] & 0x7F) << 7)

computed = [offset_of_fixed(c) for c in chunks]
check(f"fixed offset_of() on real captured headers == {expected_offsets}",
      computed == expected_offsets)

# Now feed them to the REAL assemble_chunks, in shuffled (non-arrival) order,
# to prove sorting now actually matters and produces the correct result --
# this specifically exercises the bug: before the fix, a shuffled order
# would NOT reassemble correctly, only arrival order worked "by luck".
shuffled = chunks[:]
rnd2 = random.Random(7)
rnd2.shuffle(shuffled)
assembled = gp200.assemble_chunks(shuffled)

expected_concat = b"".join(payload_by_pair[p] for p in real_pairs)
expected_decoded = gp200.nibble_decode(expected_concat)
check("assemble_chunks() on SHUFFLED real-offset chunks reassembles correctly",
      assembled == expected_decoded)

# Sanity: also check in-order (arrival order) still works, as it did before.
assembled_inorder = gp200.assemble_chunks(chunks)
check("assemble_chunks() on in-order chunks still reassembles correctly",
      assembled_inorder == expected_decoded)

# ---------------------------------------------------------------------------
# 5. build_prst_from_dump skeleton round-trip: overlaying a dump decoded
#    from the skeleton file's own content back onto itself should reproduce
#    the skeleton byte-for-byte (except the checksum field is recomputed,
#    which should still match since nothing else changed).
# ---------------------------------------------------------------------------
skeleton_bytes = bytes(gp200.resolve_skeleton_bytes(None))
CONTENT_FILE_START = gp200.CONTENT_FILE_START
CHECKSUM_OFF = gp200.CHECKSUM_OFF
# "decoded" dump content mirrors file content shifted by DUMP_TO_FILE_SHIFT;
# take the tail of the skeleton starting at CONTENT_FILE_START as if it were
# a freshly-read dump's decoded payload.
fake_dump = skeleton_bytes[CONTENT_FILE_START:]
rebuilt = gp200.build_prst_from_dump(fake_dump, "TEST", bytearray(skeleton_bytes))
check("build_prst_from_dump round trip matches skeleton except checksum recompute",
      rebuilt[:CHECKSUM_OFF] == skeleton_bytes[:CHECKSUM_OFF])
check("build_prst_from_dump recomputed checksum matches original skeleton checksum",
      rebuilt[CHECKSUM_OFF:CHECKSUM_OFF+2] == skeleton_bytes[CHECKSUM_OFF:CHECKSUM_OFF+2])
check("build_prst_from_dump output length matches skeleton length",
      len(rebuilt) == len(skeleton_bytes))

# build_prst_from_dump's "(overlaid ...)" accounting line used to print on
# EVERY call, unconditionally -- meaning once per slot on a batch export,
# 256 extra lines on a full `export --all` with nothing wrong to report.
# Gated behind a `debug` parameter as of 2026-09-29, per direct feedback
# after a real 256-slot run: "a lot of extra output that should probably be
# suppressed unless -d is used." Defaults to False (quiet), matches True.
import contextlib as _contextlib, io as _io
_buf_default = _io.StringIO()
with _contextlib.redirect_stdout(_buf_default):
    gp200.build_prst_from_dump(fake_dump, "TEST", bytearray(skeleton_bytes))
check("build_prst_from_dump: prints nothing by default (debug defaults to False)",
      _buf_default.getvalue() == "")

_buf_quiet = _io.StringIO()
with _contextlib.redirect_stdout(_buf_quiet):
    gp200.build_prst_from_dump(fake_dump, "TEST", bytearray(skeleton_bytes), debug=False)
check("build_prst_from_dump: prints nothing with debug=False explicitly",
      _buf_quiet.getvalue() == "")

_buf_debug = _io.StringIO()
with _contextlib.redirect_stdout(_buf_debug):
    gp200.build_prst_from_dump(fake_dump, "TEST", bytearray(skeleton_bytes), debug=True)
check("build_prst_from_dump: with debug=True, still prints the overlay accounting line",
      "overlaid" in _buf_debug.getvalue())

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("ALL CHECKS PASSED")
