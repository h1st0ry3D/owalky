# Owalky

## Omarchy walking tracker and walkpad controller plugin

Control an FTMS walking pad from the Omarchy bar: connect, start, pause, resume,
stop and set the speed in 0.1 km/h steps, with a live speed readout and a debug log
in the panel.

Written for pads that speak Bluetooth LE Fitness Machine Service, and developed
against a **Mobvoi Home Walking Pad** (the peripheral advertises itself as
`Mobvoi TMP`). LightBlue is not required at runtime; it is only one way to find the
pad's MAC address.

![Owalky panel](preview.png)

## Requirements

| Dependency | Why | Install |
| --- | --- | --- |
| `python-bleak` | the BLE stack the helper uses | `sudo pacman -S python-bleak` |
| `bluetoothd` | the host Bluetooth service | ships with Omarchy |
| `wl-clipboard` | optional, the panel's *Copy path* button | `sudo pacman -S wl-clipboard` |

`python-bleak` has to be the **system** package. The panel runs the helper with
`python3 -I`, which excludes the user site directory, so a `pip install --user bleak`
is not importable. The helper prints the exact command to run if it is missing.

## Install

```bash
omarchy plugin add https://github.com/h1st0ry3d/owalky
```

Then reload the shell and open the panel from the bar icon:

```bash
omarchy-shell shell rescanPlugins
omarchy-shell --reload
```

This is a `manual-setup` plugin: it needs `python-bleak` and your pad's MAC address
before it can do anything.

## Set up

1. Power-cycle the pad. It only advertises for about 30 seconds after power-on.
2. Open the panel, press **Scan**, and pick your pad from the list. It fills
   **BLE MAC address** for you. Or type the address in yourself, with any of:
   - `python3 -I owalky_helper.py scan`
   - `bluetoothctl devices | grep -i mobvoi`
   - LightBlue on iOS, or `nRF Connect` on Android
   - `bluetoothctl info <device>` while the vendor app is connected
3. Press **Connect**. The address is saved to `~/.config/owalky/config.json`.

The address is validated as a full BLE MAC before it is used, so it can never be
interpreted as anything but an address.

## Using it

- **Left click** the bar icon to open the panel, **right click** to stop the belt.
- **Walk mode** caps the belt at 6.0 km/h, which is all the pad allows while the
  handle bar is down. **Run mode** lifts the cap to 12.0. The pad does not report
  which mode it is in, so the button in the panel is the source of truth and the
  choice is remembered. Dropping from run to walk lowers the speed if it is over
  the new limit.
- **Walk mode** caps the belt at 6.0 km/h, which is all the pad allows while the
  handle bar is down. **Run mode** lifts the cap to 12.0. The pad does not report
  which mode it is in, so the button in the panel is the source of truth, and the
  choice is remembered. Dropping from run to walk lowers the speed if it is over
  the new limit.
- **Scan** listens for about 10 seconds and lists the pads that advertise the
  Bluetooth **Fitness Machine Service**, nearest first. Only FTMS devices are
  offered, so a heart rate strap or a speaker is never in the list. Press one and
  its address goes into the field above; **Connect** then acts on that address. A
  scan stores nothing by itself and never connects on its own. The pad
  advertises for about 30 seconds after power-on, so power-cycle it first.
- **Connect** starts a background daemon that claims the pad. It survives a shell
  reload, so the bar keeps working after `omarchy-shell` restarts.
- **Start Treadmill**, **Pause**, **Resume** and **Stop** act on the belt. **Stop** leaves the
  connection open, so **Start Treadmill** is immediate; **Disconnect** closes it.
- The slider picks a target speed between 1.0 and 12.0 km/h in half-kilometre steps,
  with a numbered scale underneath so a value like 3.0 can be aimed at directly. The
  step buttons move it by 0.1 or 0.5, and the mouse wheel works over the slider. The
  last speed is remembered.
- The panel shows the live speed reported by the pad, the current state, and the
  tail of the debug log.

> The bar icon shows a green dot while the belt is running, amber while paused, and
> blue while connected and idle.

### From the command line

The helper is usable on its own, which is handy when the shell is not running:

```bash
python3 -I owalky_helper.py status                     # one line of JSON
python3 -I owalky_helper.py scan                       # pads advertising FTMS, as JSON
python3 -I owalky_helper.py scan 20                    # listen for 20 seconds
python3 -I owalky_helper.py --mac AA:BB:CC:DD:EE:FF connect
python3 -I owalky_helper.py --mac AA:BB:CC:DD:EE:FF speed 3.5
echo '{"mode":"run"}' | python3 -I owalky_helper.py config-set   # lift the 6.0 cap
echo '{"mode":"run"}' | python3 -I owalky_helper.py config-set   # lift the 6.0 cap
python3 -I owalky_helper.py log 40
```

`--mac` may be omitted once an address is configured, or set with `OWALKY_MAC`.

## How it works

```
omarchy-shell (Panel.qml)
  └── owalky_helper.py status / speed / pause …      one bounded line each
        └── daemon.sock (0600, same-uid peers only)
              └── owalky/daemon.py                   the BLE link
                    └── owalky_ftms.py               the protocol codec
```

The panel never speaks Bluetooth and never reads a file. It runs the helper as an
argv array with an absolute interpreter and a cleared environment, and parses one
small JSON document per call. The helper owns a background daemon that holds the BLE
link, because the pad only accepts control writes from a central that has claimed
control, and reconnecting takes seconds.

`running` and `paused` are tracked from the commands the panel sends, not from the
pad's notifications: this pad keeps echoing an idle speed after it stops.

`docs/ftms.md` documents the opcodes, how to read them with LightBlue, and the
quirks of this particular pad.

## Security and privacy

- **No network access.** The plugin talks to the pad over Bluetooth LE and to
  nothing else. There is no telemetry, no update check and no remote configuration.
- **No privileges.** Nothing is installed, elevated or run through a shell. The
  helper runs as your user with an absolute interpreter path and a fixed environment.
- **Files written**, all `0600` inside `0700` directories:
  - `~/.config/owalky/config.json` — MAC address, last speed, walk/run mode
  - `~/.local/state/owalky/state.json` — current state
  - `~/.local/state/owalky/owalky.log` — debug log, rotated at 256 KiB
  - `~/.local/state/owalky/daemon.pid`, `daemon.sock` — daemon identity and socket
- The MAC address is a device identifier. It stays on this machine, in the files
  above. It is not in the repository and not in this README.
- Every file the plugin reads is opened through a descriptor that is checked first
  (`O_NOFOLLOW`, owner-only, one link, size limit), and every write is an exclusive
  temporary plus `rename(2)`, so a symlink planted at one of these paths cannot
  redirect a read or a write.
- Helper output is length-capped in the helper and again in QML, and rendered as
  plain text.
- The daemon is signalled by pid **and** start time, so a recycled pid is never
  signalled.

## Development

```bash
./run_tests.sh                     # unit tests, no hardware, no dependencies
./run_tests.sh --hardware --mac AA:BB:CC:DD:EE:FF   # drives the belt: do not walk on it
python3 -m unittest discover -s tests -t . -v      # same, directly
```

The unit tests cover the protocol codec, the file layer against planted symlinks,
fifos and oversized files, the state and configuration documents, process identity,
and the control socket. They need nothing but the standard library.

```
owalky_helper.py     the only executable: sets up sys.path and calls the CLI
owalky/ftms.py       the FTMS codec, pure functions, no I/O
owalky/scan.py       finding pads by the service they advertise
owalky/storage.py    directory chains, bounded reads, atomic writes, the log
owalky/state.py      state.json, config.json, MAC resolution
owalky/identity.py   /proc identity, so a pid is never trusted bare
owalky/ipc.py        the control-socket client
owalky/daemon.py     the BLE daemon, which also serves the socket
owalky/cli.py        argument parsing and the subcommands the panel calls
```

## Removing

```bash
omarchy plugin remove h1st0ry3d.owalky
```

Stop the belt and press **Disconnect** first. Removing the plugin deletes the panel
and the helper, and leaves behind:

| Path | Kept | Why |
| --- | --- | --- |
| `~/.config/owalky/config.json` | kept | your MAC address and last speed, in case you reinstall |
| `~/.local/state/owalky/owalky.log` | kept | the debug log |
| `~/.local/state/owalky/state.json` | kept | the last known state |
| `~/.local/state/owalky/daemon.pid`, `daemon.sock` | removed when the daemon exits | |

To delete those as well, remove the two directories:

```bash
rm -r ~/.config/owalky ~/.local/state/owalky
```

The daemon holds no grants, installs no packages and registers no services, so there
is nothing else to revoke.

## Licence

MIT, see [LICENSE](LICENSE).
