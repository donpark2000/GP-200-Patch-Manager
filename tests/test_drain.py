import importlib.util, time
from pathlib import Path
GP200_PATH = str(Path(__file__).resolve().parent.parent / "gp200.py")
spec = importlib.util.spec_from_file_location("gp200", GP200_PATH)
gp200 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gp200)

def new_bare_device():
    """Device.__new__ bypasses __init__ (no real MIDI ports here), so the
    handful of attributes __init__ would normally set must be filled in by
    hand. _t0 joined that list 2026-09-29 when --debug lines gained elapsed-
    time prefixes (see PROTOCOL_NOTES.md): _dbg() reads self._t0 whenever
    self.debug is True, which every scenario below sets, so every bypassed
    instance needs it now too."""
    dev = gp200.Device.__new__(gp200.Device)
    dev._t0 = time.monotonic()
    return dev

class FakeMsg:
    def __init__(self, data):
        self.type = "sysex"
        self.data = data  # bytes BETWEEN F0 and F7, as mido stores it

class FakePort:
    def __init__(self, queue):
        self.queue = list(queue)
    def receive(self, block=False):
        if self.queue:
            return self.queue.pop(0)
        return None
    def send(self, msg):
        pass
    def close(self):
        pass

def make_full(cmd, sub, off_lo, off_hi, payload_len=10):
    body = bytearray()
    body += gp200.HEADER[1:]  # data is BETWEEN F0/F7, mido excludes F0 itself... but our HEADER includes F0
    return None

def make_data_between(cmd, sub, off_lo, off_hi, payload_len=10):
    # Build the full F0..F7 message, then strip the F0/F7 like mido's msg.data would hold
    full = bytearray()
    full += gp200.HEADER          # F0 ... (8 bytes incl F0)
    full.append(cmd)
    full.append(sub)
    full.append(0x00)             # slot byte (irrelevant for this test)
    full.append(off_lo)
    full.append(off_hi)
    full += bytes([0x01] * payload_len)
    full.append(0xF7)
    return bytes(full[1:-1])      # strip F0 and F7, as mido.Message.data would hold

# Scenario 1: read_name style (require_offset0=True). Queue has a stray
# offset!=0 echo FIRST, then the real offset-0 response.
stray = FakeMsg(make_data_between(0x12, 0x18, 0x39, 0x01))   # offset 185 stray
real  = FakeMsg(make_data_between(0x12, 0x18, 0x00, 0x00))   # offset 0, the real name chunk

dev = new_bare_device()
dev.debug = True
dev.inport = FakePort([stray, real])

result = dev._drain_matching(0x12, 0x18, 1, timeout_s=1.0, require_offset0=True)
print("Scenario 1 result:", None if result is None else [r.hex() for r in result])
assert result is not None and len(result) == 1
assert result[0][11] == 0 and result[0][12] == 0
print("PASS: stray offset!=0 chunk was correctly skipped, real offset-0 chunk accepted\n")

# Scenario 2: full 7-chunk read where a duplicate offset (stale echo of an
# earlier offset) shows up mixed in with the 7 real, unique offsets.
offsets = [(0x00,0x00),(0x39,0x01),(0x72,0x02),(0x2b,0x04),(0x64,0x05),(0x1d,0x07),(0x56,0x08)]
queue = [FakeMsg(make_data_between(0x12, 0x18, 0x39, 0x01))]  # duplicate/stray of offset #2, arrives first
queue += [FakeMsg(make_data_between(0x12, 0x18, lo, hi)) for lo, hi in offsets]
dev2 = new_bare_device()
dev2.debug = True
dev2.inport = FakePort(queue)
result2 = dev2._drain_matching(0x12, 0x18, 7, timeout_s=1.0)
print("Scenario 2: got", None if result2 is None else len(result2), "chunks")
assert result2 is not None and len(result2) == 7
got_offsets = sorted((m[11], m[12]) for m in result2)
assert got_offsets == sorted(offsets), (got_offsets, sorted(offsets))
print("PASS: duplicate offset was ignored, all 7 real unique offsets collected\n")

# Scenario 3: _flush_pending drains everything currently queued.
dev3 = new_bare_device()
dev3.debug = True
dev3.inport = FakePort([FakeMsg(b"\x00"*3) for _ in range(4)])
dev3._flush_pending()
assert dev3.inport.receive() is None
print("PASS: _flush_pending drains the queue\n")

print("ALL DRAIN-LOGIC CHECKS PASSED")
