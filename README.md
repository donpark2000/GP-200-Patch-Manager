# GP-200 Patch Manager

A command-line tool for **bulk backup and restore of Valeton GP-200 patches**,
talking live SysEx over USB-MIDI directly to the pedal.

It doesn't know or care about effects, amps, or cabs -- it only moves whole
`.prst` patches between the pedal and files on disk. If you just want to back
up your whole bank, push a collection of patches onto a fresh device, or pull
one patch back down, this is a lightweight way to do it without a full
editor.

## Why this exists

There are already excellent tools in this space -- [GP200 Studio](https://github.com/kabir0st/gp200-studio)
(a full patch editor) and [RigSheet](https://github.com/ricardo-mv/rigsheet)
(a web-based cheat-sheet/reference) both do real, valuable work here. This
tool isn't trying to replace either of them or duplicate patch-creation
features that already work fine. It covers a narrower gap: bulk,
scriptable backup/restore, plus a set of diagnostic commands for
characterizing how reliable the USB-MIDI link actually is (see
`PROTOCOL_NOTES.md`) -- something that turned out to matter more than
expected once real hardware got involved.

## Status

- **Read/export path**: tested and trustworthy against real hardware.
- **Write path**: has gone through several rounds of real-hardware testing
  and a cross-check against a second independent reverse-engineering of the
  protocol. `--method live` is confirmed to persist real changes, with a
  known bug affecting a few effect parameter types. The default `--method
  flash` (chunk upload) has not yet been re-confirmed against real hardware
  after a recent fix -- see the top of `gp200.py` and `PROTOCOL_NOTES.md`
  for the full, current, honest picture before trusting it with patches you
  care about.

This is an actively evolving personal project, not a finished product --
`PROTOCOL_NOTES.md` tracks what's confirmed, what's still open, and the
evidence behind each finding.

## Requirements

- Python 3.12 (developed and tested against this version specifically)
- [`mido`](https://pypi.org/project/mido/) and
  [`python-rtmidi`](https://pypi.org/project/python-rtmidi/)
- A USB-MIDI connection to a Valeton GP-200

```bash
pip install mido python-rtmidi
```

(A standalone executable that bundles Python and these dependencies -- no
install required -- is planned; see the Roadmap below.)

## Usage

```
gp200.py list-ports                          # show every MIDI port Python can see
gp200.py list                                # print the name in every one of the 256 slots
gp200.py read 34-B                           # print one slot's name (fast, no writing)

gp200.py export 34-B                         # save one slot to a .prst file
gp200.py export --all                        # save all 256 slots to a .zip
gp200.py export --start 34-A --end 36-D      # save a slot range to a .zip

gp200.py upload patch.prst 34-B              # write one file to a slot
gp200.py upload a.prst b.prst c.prst 10-A    # write several files to consecutive slots
gp200.py upload backup.zip 10-A              # write every file in a zip to consecutive slots
gp200.py apply-template patch.prst 30-A 34-D # write ONE file into every slot in a range

gp200.py reread 37-A                         # repeated no-write reads, to isolate read-side noise
gp200.py raw-sweep --all                     # compare a Studio-style single read against a
                                              # confirmed (multi-read-agreement) read, across slots
gp200.py soak 37-A patch.prst --count 20     # repeated write+verify cycles at a fixed delay,
                                              # for comparing hardware/cabling conditions
gp200.py calibrate-settle 37-A patch.prst    # find the settle delay that stops verify retries
gp200.py diag-write 34-B --template t.prst   # high-slot addressing test, with automatic backup
```

Every command supports `-d`/`--debug` for a full SysEx trace, and
`--log-file PATH` to save a complete copy of a run (environment info, MIDI
ports seen, full trace) to a file -- handy if something needs to be reported
or diagnosed later. Run `gp200.py <command> --help` for full details and
every option on any command above.

## Testing

A hardware-free regression suite covers every command (fake/mock MIDI
devices -- no GP-200 needs to be plugged in):

```bash
python run_tests.py
```

Runs every `tests/test_*.py` file and reports one pass/fail verdict. Run it
before and after any change.

## Protocol notes

`PROTOCOL_NOTES.md` is the running log of everything learned about the
GP-200's SysEx protocol along the way -- findings, evidence, what's still
open, and where each mechanism came from. Kept up to date as the project
evolves, not just written once.

**Credit**: the core SysEx message formats (read requests, upload chunking,
preset-change messages, the nibble encoding) are ported from Kabir S.
Tamari's [GP200 Studio](https://github.com/kabir0st/gp200-studio) (GPL-3.0),
which reverse-engineered them from USB captures of Valeton's own editor.
The flash-chunk upload addressing was additionally cross-checked against a
second, independent reverse-engineering in [RigSheet](https://github.com/ricardo-mv/rigsheet).

## Roadmap

- Standalone executable (PyInstaller) so end users don't need Python or
  `mido`/`python-rtmidi` installed at all.
- Re-confirm the default flash-chunk write path against real hardware after
  its recent addressing fix.

## Contributing / Issues

Bug reports, real-hardware test results, and pull requests are all welcome
via [GitHub Issues](../../issues).

## License

[GPL-3.0](LICENSE) -- if you build on this, changes and derivatives should
stay open too.
