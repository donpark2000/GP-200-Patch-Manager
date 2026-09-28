import importlib.util, os, shutil, tempfile
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

real_skeleton = gp200.DEFAULT_SKELETON.read_bytes()

# 1. the embedded copy must be byte-identical to the real skeleton.prst on disk
embedded = gp200.resolve_skeleton_bytes(None)
check("embedded skeleton matches skeleton.prst on disk (DEFAULT_SKELETON present)",
      bytes(embedded) == real_skeleton)

# 2. explicit --skeleton path still wins and is validated
explicit = gp200.resolve_skeleton_bytes(str(gp200.DEFAULT_SKELETON))
check("explicit skeleton path resolves correctly", bytes(explicit) == real_skeleton)

# 3. simulate skeleton.prst being deleted: temporarily rename it away, then
#    resolve_skeleton_bytes(None) must still succeed via the embedded copy.
tmp_hidden = gp200.DEFAULT_SKELETON.with_suffix(".hidden_for_test")
shutil.move(gp200.DEFAULT_SKELETON, tmp_hidden)
try:
    check("skeleton.prst really is gone for this test", not gp200.DEFAULT_SKELETON.exists())
    fallback = gp200.resolve_skeleton_bytes(None)
    check("resolve_skeleton_bytes(None) falls back to the embedded copy when the file is missing",
          bytes(fallback) == real_skeleton)
    check("fallback bytes are a structurally valid .prst (magic + length)",
          len(fallback) == gp200.PRST_LEN and bytes(fallback[0:4]) == b"TSRP")
finally:
    shutil.move(tmp_hidden, gp200.DEFAULT_SKELETON)

check("skeleton.prst restored after the test", gp200.DEFAULT_SKELETON.exists())

print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("ALL EMBEDDED-SKELETON CHECKS PASSED")
