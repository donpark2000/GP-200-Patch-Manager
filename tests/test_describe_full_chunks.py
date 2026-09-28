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


def make_sysex(cmd, sub, body_len):
    body = bytes(range(body_len % 256)) if body_len < 256 else (bytes(range(256)) * (body_len // 256 + 1))[:body_len]
    return gp200.HEADER + bytes([cmd, sub]) + body + b"\xf7"


# --- read-chunk responses (cmd=0x12/sub=0x18) are shown in FULL, not
#     truncated to 24 bytes -- needed to actually compare wire bytes deep
#     inside a 384-byte chunk, which is exactly where a real mismatch was
#     found (well past the old 24-byte preview window). ---
big_chunk = make_sysex(0x12, 0x18, 374)  # a realistic ~384-byte-total chunk
desc = gp200._describe(big_chunk)
check("_describe: a 0x12/0x18 chunk message is NOT truncated",
      "more)" not in desc)
check("_describe: the full body hex appears in the description",
      big_chunk[10:-1].hex(" ") in desc)

# --- other GP-200 message types keep the old 24-byte truncation, so normal
#     -d output for non-chunk traffic (handshake, write commands, etc.)
#     doesn't balloon in size unnecessarily. ---
other_msg = make_sysex(0x11, 0x10, 50)
desc2 = gp200._describe(other_msg)
check("_describe: a non-chunk GP-200 message is still truncated to 24 bytes",
      "more)" in desc2)

print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("ALL DESCRIBE-FULL-CHUNKS CHECKS PASSED")
