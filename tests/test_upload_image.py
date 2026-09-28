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

skeleton_path = gp200.DEFAULT_SKELETON if gp200.DEFAULT_SKELETON.exists() else None
if skeleton_path is None:
    raise SystemExit("no default skeleton available to test against")
file_bytes = bytearray(skeleton_path.read_bytes())
# Put a recognizable marker at 0x2E/0x2F so we can confirm those bytes now
# actually make it into the transmitted payload (they used to be dropped).
file_bytes[0x2E] = 0xAB
file_bytes[0x2F] = 0xCD
file_bytes = bytes(file_bytes)

slot = 133  # an arbitrary >127 slot to make sure it's not accidentally masked
image = gp200.build_upload_image(file_bytes, slot)

check("image length == 1184", len(image) == 1184)
check("header[6] == slot (not 0xFF)", image[6] == slot & 0xFF)
check("header[12] == slot (not 0xFF)", image[12] == slot & 0xFF)
check("header[0:6] fixed prefix", list(image[0:6]) == [0,0,4,0,1,0])
check("header[8:12] fixed mid section", list(image[8:12]) == [1,0,4,0])
check("content offset 0x2E/0x2F now present at image[14:16]",
      image[14] == 0xAB and image[15] == 0xCD)
check("file offset 0x34 (image[20]) blanked to 0xFF", image[20] == 0xFF)
check("file offset 0x90 (image[112]) blanked to 0xFF", image[112] == 0xFF)
# sanity: image[14+k] should equal file_bytes[0x2E+k] for k not in the two
# blanked positions (6 -> file 0x34, 98 -> file 0x90)
mismatches = []
for k in range(len(image) - 14):
    if k in (6, 98):
        continue
    want = file_bytes[0x2E + k]
    got = image[14 + k]
    if want != got:
        mismatches.append((k, want, got))
check("all other content bytes pass through unchanged", not mismatches)
if mismatches:
    print("  first few mismatches:", mismatches[:5])

chunks = gp200.build_upload_chunks(image, slot)
check("7 chunks produced", len(chunks) == 7)
for i, c in enumerate(chunks):
    check(f"chunk {i}: starts with HEADER", c[0:8] == gp200.HEADER)
    check(f"chunk {i}: cmd/sub == 0x12/0x20", c[8] == 0x12 and c[9] == 0x20)
    check(f"chunk {i}: outer header byte[10] == 0x09 (fixed, not slot)", c[10] == 0x09)
    check(f"chunk {i}: ends with 0xF7", c[-1] == 0xF7)
    check(f"chunk {i}: all data bytes SysEx-safe (<=0x7F)", all(b <= 0x7F for b in c[1:-1]))

# offsets should be monotonically increasing multiples of 183, last one short
offsets = [ (c[11] & 0x7F) | ((c[12] & 0x7F) << 7) for c in chunks ]
check("chunk offsets are 0,183,366,...", offsets == [i*183 for i in range(len(chunks))])

# round-trip: decode the nibble payload of each chunk and reassemble, compare to image
reassembled = b"".join(gp200.nibble_decode(c[13:-1]) for c in chunks)
check("reassembled chunks == original image", reassembled == image)

print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("ALL UPLOAD-IMAGE CHECKS PASSED")
