import importlib.util
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

# Build a "device dump" whose tail block bytes, 0x2E, and the newly-found
# STUDIO_ADDITIONAL_ZEROED_OFFSETS positions are all deliberately non-zero,
# the way a real device dump often is (that's the whole finding).
dump = bytearray(skeleton)
for off in gp200.TAIL_BLOCK_FILE_OFFSETS:
    dump[off] = 0xAB
dump[gp200.EXPORT_ZEROED_SLOT_ECHO_OFFSET] = 0x90  # e.g. a real slot value
for off in gp200.STUDIO_ADDITIONAL_ZEROED_OFFSETS:
    dump[off] = 0x64  # 100 -- matches the real value seen at 0x3E/0x40 (2026-09-27)
# slot-mirror bytes that must NOT be zeroed by this function
dump[0x34] = 0x90
dump[0x90] = 0x90
# and a real, non-ignored content byte that should survive untouched
dump[0x44] = ord('X')
dump = bytes(dump)[gp200.CONTENT_FILE_START:gp200.CHECKSUM_OFF]

built = gp200.build_prst_from_dump(dump, "x", skeleton)
check("sanity: build_prst_from_dump alone does NOT zero the tail block",
      any(built[off] == 0xAB for off in gp200.TAIL_BLOCK_FILE_OFFSETS))

normalized = gp200.normalize_export_dynamic_fields(built)

check("normalize_export_dynamic_fields: every tail-block byte is zeroed",
      all(normalized[off] == 0x00 for off in gp200.TAIL_BLOCK_FILE_OFFSETS))
check("normalize_export_dynamic_fields: the 0x2E slot-echo byte is zeroed",
      normalized[gp200.EXPORT_ZEROED_SLOT_ECHO_OFFSET] == 0x00)
check("normalize_export_dynamic_fields: every STUDIO_ADDITIONAL_ZEROED_OFFSETS "
      "position (0x3E/0x40 + the tail-quad +5/+10/+11 bytes) is zeroed",
      all(normalized[off] == 0x00 for off in gp200.STUDIO_ADDITIONAL_ZEROED_OFFSETS))
check("STUDIO_ADDITIONAL_ZEROED_OFFSETS: exactly 26 positions (2 standalone + "
      "3 per quad x 8 quads)",
      len(gp200.STUDIO_ADDITIONAL_ZEROED_OFFSETS) == 26)
check("STUDIO_ADDITIONAL_ZEROED_OFFSETS: doesn't overlap TAIL_BLOCK_FILE_OFFSETS "
      "or EXPORT_ZEROED_SLOT_ECHO_OFFSET (distinct positions, not double-counted)",
      not (gp200.STUDIO_ADDITIONAL_ZEROED_OFFSETS & gp200.TAIL_BLOCK_FILE_OFFSETS) and
      gp200.EXPORT_ZEROED_SLOT_ECHO_OFFSET not in gp200.STUDIO_ADDITIONAL_ZEROED_OFFSETS)
check("normalize_export_dynamic_fields: the OTHER slot-mirror bytes (0x34, 0x90) "
      "are deliberately left alone -- real Valeton exports don't zero these",
      normalized[0x34] == 0x90 and normalized[0x90] == 0x90)
check("normalize_export_dynamic_fields: real content elsewhere is untouched",
      normalized[0x44] == ord('X'))
check("normalize_export_dynamic_fields: file length is unchanged",
      len(normalized) == len(built))
check("normalize_export_dynamic_fields: checksum is recomputed to match the zeroed content",
      gp200.struct.unpack_from(">H", normalized, gp200.CHECKSUM_OFF)[0] == gp200.prst_checksum(normalized))
check("normalize_export_dynamic_fields: checksum actually changed vs. the un-zeroed version "
      "(proves it didn't just reuse the old checksum)",
      gp200.struct.unpack_from(">H", normalized, gp200.CHECKSUM_OFF)[0] !=
      gp200.struct.unpack_from(">H", built, gp200.CHECKSUM_OFF)[0])

# Two dumps differing ONLY in the tail block and the 0x2E slot-echo byte
# must normalize to the SAME exported bytes -- the whole point (reproducible
# exports of an unchanged patch, matching what a Valeton export would also
# show).
dump2 = bytearray(skeleton)
for off in gp200.TAIL_BLOCK_FILE_OFFSETS:
    dump2[off] = 0xCD  # different tail-block noise, same real content
dump2[gp200.EXPORT_ZEROED_SLOT_ECHO_OFFSET] = 0x37  # different slot-echo noise
dump2[0x34] = 0x90
dump2[0x90] = 0x90
dump2[0x44] = ord('X')
dump2 = bytes(dump2)[gp200.CONTENT_FILE_START:gp200.CHECKSUM_OFF]
normalized2 = gp200.normalize_export_dynamic_fields(gp200.build_prst_from_dump(dump2, "x", skeleton))
check("normalize_export_dynamic_fields: two reads differing only in tail-block noise "
      "produce byte-IDENTICAL exports",
      normalized == normalized2)

# verify_write_full / diff_prst_content must NOT be affected by the
# TAIL_BLOCK_FILE_OFFSETS/0x2E zeroing -- those are already excluded via
# VERIFY_IGNORE_OFFSETS, so write verification keeps comparing the device's
# real, unmodified reported bytes there regardless of what export does.
# STUDIO_ADDITIONAL_ZEROED_OFFSETS is deliberately NOT added to
# VERIFY_IGNORE_OFFSETS (same reasoning as EXPORT_ZEROED_SLOT_ECHO_OFFSET:
# this was found by comparing FILES, not by testing the write path, so it
# says nothing about what a correct write should echo back) -- so those
# positions SHOULD show up as mismatches here, proving the write-verification
# path is untouched by this export-only change.
mismatches = gp200.diff_prst_content(built, normalized)
mismatch_offsets = {off for off, _, _ in mismatches}
check("normalize_export_dynamic_fields's TAIL_BLOCK_FILE_OFFSETS/0x2E zeroing "
      "is invisible to diff_prst_content (already ignored there)",
      not (mismatch_offsets & (gp200.TAIL_BLOCK_FILE_OFFSETS | {gp200.EXPORT_ZEROED_SLOT_ECHO_OFFSET})))
check("normalize_export_dynamic_fields's STUDIO_ADDITIONAL_ZEROED_OFFSETS zeroing "
      "is NOT hidden from diff_prst_content -- write verification is deliberately "
      "left untouched by this export-only finding",
      mismatch_offsets == gp200.STUDIO_ADDITIONAL_ZEROED_OFFSETS)

print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("ALL EXPORT-NORMALIZATION CHECKS PASSED")
