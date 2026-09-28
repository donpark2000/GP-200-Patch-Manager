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

# --- Length / framing sanity for every new builder ---
tests = [
    ("build_toggle_effect", gp200.build_toggle_effect(3, True), 46),
    ("build_toggle_effect(off)", gp200.build_toggle_effect(10, False), 46),
    ("build_effect_change", gp200.build_effect_change(3, 0x0B000004), 54),
    ("build_param_change", gp200.build_param_change(3, 0, 0x0B000004, 50.0), 62),
    ("build_patch_setting", gp200.build_patch_setting(0x00, 50), 46),
    ("build_reorder_effects", gp200.build_reorder_effects(list(range(11)), 4, 4), 78),
    ("build_save_commit", gp200.build_save_commit("JCM800 Recipe", 133), 62),
    ("build_author_name", gp200.build_author_name("Don"), 78),
]
for name, msg, want_len in tests:
    check(f"{name}: length == {want_len}", len(msg) == want_len)
    check(f"{name}: starts with HEADER", msg[:8] == gp200.HEADER)
    check(f"{name}: ends with 0xF7", msg[-1] == 0xF7)
    check(f"{name}: all data bytes (excluding F0/F7 framing) <= 0x7F (SysEx-safe)",
          all(b <= 0x7F for b in msg[1:-1]))

# --- Spot-check exact byte layout against the reference (hand-verified indices) ---
msg = gp200.build_toggle_effect(3, True)
check("toggle: cmd/sub = 0x12/0x10", msg[8] == 0x12 and msg[9] == 0x10)
check("toggle: block index at [38]", msg[38] == 3)
check("toggle: enabled at [40]", msg[40] == 1)
msg_off = gp200.build_toggle_effect(3, False)
check("toggle(off): enabled at [40] == 0", msg_off[40] == 0)

msg = gp200.build_effect_change(3, 0x0B000004)
check("effect_change: cmd/sub = 0x12/0x14", msg[8] == 0x12 and msg[9] == 0x14)
check("effect_change: block index at [38]", msg[38] == 3)
check("effect_change: module type at [52] == 0x0B", msg[52] == 0x0B)
check("effect_change: variant nibbles at [45:47] == (0,4)", msg[45] == 0 and msg[46] == 4)

msg = gp200.build_patch_setting(0x00, 50)
check("patch_setting: cmd/sub = 0x12/0x10", msg[8] == 0x12 and msg[9] == 0x10)
check("patch_setting: target at [38]", msg[38] == 0x00)
# value 50 = 0x32 -> high nibble 3, low nibble 2
check("patch_setting: value nibbles at [41:43] == (3,2)", msg[41] == 3 and msg[42] == 2)

msg_pan_left = gp200.build_patch_setting(0x06, 200)  # >127 => pan-left special case
check("patch_setting PAN-left: [43:45] == (0x0F,0x0F)", msg_pan_left[43] == 0x0F and msg_pan_left[44] == 0x0F)

msg_tempo_big = gp200.build_patch_setting(0x01, 300)  # >255 => extended nibble encoding
check("patch_setting tempo>255 special-case applied",
      msg_tempo_big[41] == (300 >> 4) & 0xF and msg_tempo_big[42] == 300 & 0xF and
      msg_tempo_big[43] == (300 >> 12) & 0xF and msg_tempo_big[44] == (300 >> 8) & 0xF)

# --- build_param_change: decode the nibble-encoded payload back and check fields ---
msg = gp200.build_param_change(5, 2, 0x0B000004, 33.5)
nibble_payload = msg[13:-1]
decoded = gp200.nibble_decode(nibble_payload)
check("param_change: decoded length == 24", len(decoded) == 24)
check("param_change: decoded[12] == block index", decoded[12] == 5)
check("param_change: decoded[13] == param index", decoded[13] == 2)
import struct
(got_effect_id,) = struct.unpack_from("<I", decoded, 16)
(got_value,) = struct.unpack_from("<f", decoded, 20)
check("param_change: round-tripped effect_id", got_effect_id == 0x0B000004)
check("param_change: round-tripped float value", abs(got_value - 33.5) < 1e-4)

# --- build_reorder_effects: decode and check ---
order = [3, 0, 1, 2, 4, 5, 6, 7, 8, 9, 10]
msg = gp200.build_reorder_effects(order, 4, 6)
decoded = gp200.nibble_decode(msg[13:-1])
check("reorder: decoded length == 32", len(decoded) == 32)
check("reorder: send/return at [14:16]", decoded[14] == 4 and decoded[15] == 6)
check("reorder: order at [16:27]", list(decoded[16:27]) == order)
check("reorder: terminator at [27] == 0x44", decoded[27] == 0x44)

# --- build_save_commit: decode and check ---
msg = gp200.build_save_commit("JCM800 Recipe", 133)
decoded = gp200.nibble_decode(msg[13:-1])
check("save_commit: decoded length == 24", len(decoded) == 24)
check("save_commit: header bytes [0:3] == 03 20 14", list(decoded[0:3]) == [0x03, 0x20, 0x14])
check("save_commit: slot at [4]", decoded[4] == 133)
check("save_commit: name at [8:24]", decoded[8:8+13] == b"JCM800 Recipe")

# --- encode_display_value sanity (monotonic, 0 for non-positive) ---
check("encode_display_value(0) == b'\\x00\\x00'", gp200.encode_display_value(0) == b"\x00\x00")
lo = gp200.encode_display_value(1.0)
hi = gp200.encode_display_value(100.0)
lo_u16 = lo[0] | (lo[1] << 8)
hi_u16 = hi[0] | (hi[1] << 8)
check("encode_display_value is monotonically increasing with value", hi_u16 > lo_u16)

# --- decode_prst against the real skeleton file ---
preset = gp200.decode_prst(bytes(gp200.resolve_skeleton_bytes(None)))
check("decode_prst: patchName is non-empty", len(preset["patchName"]) > 0)
check("decode_prst: exactly 11 effects", len(preset["effects"]) == 11)
slot_indices = sorted(e["slotIndex"] for e in preset["effects"])
check("decode_prst: effects' slotIndex is a 0..10 permutation", slot_indices == list(range(11)))
check("decode_prst: each effect has 15 params", all(len(e["params"]) == 15 for e in preset["effects"]))
check("decode_prst: all params finite", all(all(isinstance(p, float) and p == p and abs(p) != float("inf")
                                                 for p in e["params"]) for e in preset["effects"]))
check("decode_prst: fxLoopSend/Return in 1..11", 1 <= preset["fxLoopSend"] <= 11 and 1 <= preset["fxLoopReturn"] <= 11)
check("decode_prst: patchVolume in 0..100", 0 <= preset["patchVolume"] <= 100)
print("\nDecoded skeleton preset summary:")
print(f"  name={preset['patchName']!r} author={preset['author']!r}")
print(f"  fxLoopSend={preset['fxLoopSend']} fxLoopReturn={preset['fxLoopReturn']}")
print(f"  patchVolume={preset['patchVolume']} patchPan={preset['patchPan']} patchTempo={preset['patchTempo']}")
for e in preset["effects"]:
    print(f"  block {e['slotIndex']:2d}: effectId=0x{e['effectId']:08X} enabled={e['enabled']}")

print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("ALL LIVE-WRITE CHECKS PASSED")
