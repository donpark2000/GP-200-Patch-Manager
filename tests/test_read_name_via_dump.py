"""Real-hardware evidence (2026-09-29, see PROTOCOL_NOTES.md, "In progress:
why does list feel slower than export --all?") found the name-only request
`read_name` uses (sub=0x20) reliably times out its FULL budget on the first
attempt before succeeding on retry -- on every single slot in two complete
traces, zero exceptions -- while the full-dump request `read_dump` uses
(sub=0x10, the same one `export` relies on) succeeded on its first attempt
in under 20ms, also with zero exceptions. `list` switched from `read_name`
to the new `read_name_via_dump` (read_dump + extract_name_field, discarding
everything but the name) because of that.

Covers: read_name_via_dump returns the same name a real dump's name field
would decode to, it's read_dump (unconfirmed, single-attempt-by-default)
underneath rather than the multi-read-agreement read_dump_confirmed (a
wrong name once in a rare while is cosmetic, not worth the extra round
trips read_dump_confirmed spends catching real corruption), and a
TimeoutError from the underlying read_dump propagates unchanged rather than
being swallowed.
"""
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


def make_decoded_with_name(name: str) -> bytes:
    """A minimal fake 'decoded' dump: just long enough to hold the 16-byte
    name field at its real offset (28:44), zeroed elsewhere -- everything
    read_name_via_dump touches beyond that offset is irrelevant to it."""
    buf = bytearray(200)
    name_bytes = name.encode("ascii")[:16]
    buf[28:28 + len(name_bytes)] = name_bytes
    return bytes(buf)


dev = gp200.Device.__new__(gp200.Device)  # bypass __init__, no real MIDI port
calls = {"read_dump": 0, "read_dump_confirmed": 0}


def fake_read_dump(slot, retries=gp200.RETRY_COUNT):
    calls["read_dump"] += 1
    return make_decoded_with_name("Hi Sweety")


def fake_read_dump_confirmed(slot, tries=5):
    calls["read_dump_confirmed"] += 1
    raise AssertionError("read_name_via_dump must not use the confirmed/"
                          "multi-read path -- that's for real backups, not "
                          "a cosmetic slot label")


dev.read_dump = fake_read_dump
dev.read_dump_confirmed = fake_read_dump_confirmed

result = dev.read_name_via_dump(0)
check("read_name_via_dump extracts the correct name from the dump",
      result == "Hi Sweety")
check("read_name_via_dump calls read_dump exactly once",
      calls["read_dump"] == 1)
check("read_name_via_dump never touches read_dump_confirmed",
      calls["read_dump_confirmed"] == 0)


# --- a genuine failure (device didn't answer at all) must propagate as
#     TimeoutError, the same contract read_name already had -- cmd_list's
#     `except TimeoutError: name = "(no response)"` depends on this. ---
def failing_read_dump(slot, retries=gp200.RETRY_COUNT):
    raise TimeoutError(f"no response reading slot {gp200.slot_to_label(slot)}")


dev.read_dump = failing_read_dump
try:
    dev.read_name_via_dump(5)
    raised = False
except TimeoutError:
    raised = True
check("read_name_via_dump propagates TimeoutError from read_dump unchanged",
      raised)


print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("ALL read_name_via_dump CHECKS PASSED")
