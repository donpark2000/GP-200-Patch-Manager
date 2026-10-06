# GP-200 Patch Manager

[![CI](https://github.com/donpark2000/GP-200-Patch-Manager/actions/workflows/ci.yml/badge.svg)](https://github.com/donpark2000/GP-200-Patch-Manager/actions/workflows/ci.yml)

A command-line tool for **bulk backup and restore of Valeton GP-200 patches**,
talking live SysEx over USB-MIDI directly to the pedal.

It doesn't know or care about effects, amps, or cabs -- it only moves whole
`.prst` patches between the pedal and files on disk. If you just want to back
up your whole bank, push a collection of patches onto a fresh device, or pull
one patch back down, this is a lightweight way to do it without a full
editor.

**Most people should use the web version instead:
[GP-200 Patch Manager Web](https://donpark2000.github.io/GP-200-Patch-Manager-Web/)**
([source](https://github.com/donpark2000/GP-200-Patch-Manager-Web)). It
does the same backup and restore in Chrome or Edge, with nothing to
install, and it's much faster: a full restore of all 256 slots takes about
30 seconds there, against about 8 minutes here. The speed comes from what
building these two tools taught us about the pedal (see
[LESSONS.md](https://github.com/donpark2000/GP-200-Patch-Manager-Web/blob/main/LESSONS.md));
those changes haven't been brought back to this command-line version. Use
this one when you need a command line, for example to run backups from a
script.

## Download

If you need the command line, grab the pre-built executable for your OS --
no Python install needed. Get the latest one from the
**[Releases page](../../releases/tag/latest)** ("Latest build", auto-updated
by CI on every change to `main`):

| Your OS | Download | Before it'll run |
|---|---|---|
| Windows | `gp200-windows.exe` | Nothing extra -- just run it. Windows SmartScreen may warn about an unrecognized app the first time; click "More info" -> "Run anyway". **Confirmed working** on a real machine with no Python installed. |
| Linux | `gp200-linux` | Make it executable first: `chmod +x gp200-linux`. Built on Ubuntu 22.04 -- works on anything with an equal or newer glibc (confirmed on Bodhi Linux 7). A noticeably older distro may need building from source instead (see below). |
| macOS | `gp200-macos` | macOS blocks unsigned apps by default (this one isn't code-signed). If you get a "can't be opened" message: right-click the file -> Open -> confirm, or run `xattr -d com.apple.quarantine ./gp200-macos` in Terminal first. **Not yet verified on a real Mac** -- please open an issue if it doesn't work for you. |

Once you have it, jump to [Usage](#usage) below -- the commands are identical
whether you're running the executable or the Python script.

If you want to modify the code, or the executable doesn't work for your
system, see [Building from source](#building-from-source).

## Why this exists

There's already an excellent editor in this space --
[GP200 Studio](https://gp200studio.com/), a full patch editor
([source](https://github.com/kabir0st/gp200-studio)). This tool isn't
trying to replace it or duplicate patch-creation features that already
work fine. It covers a narrower gap: bulk,
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

## Known limitations

**Patches referencing a User-IR or NAM ("SnapTone") slot don't carry that
content.** A patch only stores a small number saying *which* User-IR or
SnapTone slot to use for a cab/amp/drive -- never the impulse response or
NAM capture itself (see `PROTOCOL_NOTES.md`, Finding 11). Exporting and
restoring such a patch on the *same* device, with nothing reloaded into
that slot since, sounds exactly right. Moving it to a *different* device --
or reloading that slot with something else on the same one -- makes the
patch silently pick up whatever is there now, with no warning either way.
Even manually re-loading the original IR/NAM file isn't enough by itself:
it has to land back in the *same slot number* the patch references, or the
patch breaks just the same as if the content were missing or swapped.

This is a property of the `.prst` format itself, confirmed by reading
GP200 Studio's own patch-export code: it has the identical exposure, and
as far as we've found, no GP-200 tool (including Valeton's own editor)
backs up or moves the actual IR/NAM slot content. If you're moving patches
between devices, or seeing sound differences after a restore, this is
almost certainly why.

## Usage

The commands below are written as `gp200.py ...` (running from source with
Python), but they're identical either way -- if you're using a downloaded
executable, just swap in `./gp200-linux`, `./gp200-macos`, or `gp200.exe`
(or `.\gp200.exe` in PowerShell) in place of `gp200.py` in every example.

```
gp200.py list-ports                          # show every MIDI port Python can see
gp200.py list                                # print the name in every one of the 256 slots
gp200.py read 34-B                           # print one slot's name (fast, no writing)

gp200.py export 34-B                         # save one slot to a .prst file
gp200.py export --all                        # save all 256 slots to a .zip
gp200.py export --start 34-A --end 36-D      # save a slot range to a .zip

gp200.py upload patch.prst 34-B              # write one file to a slot
gp200.py upload a.prst b.prst c.prst 10-A    # write several files, filling 10-A, 10-B, 10-C...
gp200.py upload backup.zip 10-A              # write every file in a zip, filling from 10-A on
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

**Restoring a backup**: a multi-file or zip `upload` always fills
*consecutive* slots starting at the one you give it -- it does not read each
patch's original slot back out of the zip. To put a backup back where it
came from, upload it to the *same* slot you exported it from (a zip made
with `export --all` starts at `1-A`, so it goes back with `upload
backup.zip 1-A`). If that export printed a `skipped` warning for any slot,
the zip has a gap that won't be preserved on restore -- run `export` again
first to fill it in. See `gp200.py upload --help` for the full explanation.

## Building from source

Only needed if you want to modify the code, or a downloaded executable
doesn't work on your system.

- Python 3.12 (developed and tested against this version specifically)
- [`mido`](https://pypi.org/project/mido/) and
  [`python-rtmidi`](https://pypi.org/project/python-rtmidi/)
- A USB-MIDI connection to a Valeton GP-200

```bash
pip install mido python-rtmidi
python gp200.py list-ports
```

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

- Confirm the macOS build actually runs on a real Mac (untested so far --
  see [Download](#download)).
- Proper versioned releases (`v0.1.0`, etc.) once the project is stable
  enough to mean something by a version number, rather than just the
  rolling "latest" build every push produces today.
- Re-confirm the default flash-chunk write path against real hardware after
  its recent addressing fix.

## Contributing / Issues

Bug reports, real-hardware test results, and pull requests are all welcome
via [GitHub Issues](../../issues).

## License

[GPL-3.0](LICENSE) -- if you build on this, changes and derivatives should
stay open too.
