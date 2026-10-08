# btkeyboardonly

Using a phone as a Bluetooth keyboard should let the keyboard reconnect without asking for approval every time, while calls and media audio stay on the phone. But BlueZ trusts an entire device, and Blueman's “Always accept” button enables that whole-device trust—even when the authorization prompt is for the keyboard service. Trusting the keyboard therefore also permits the phone's audio services, which can cause sound to be routed to the computer.

`btkeyboardonly` provides selective service authorization: automatically approve classic Bluetooth HID connections from selected paired devices and reject **all other service authorization requests** from those devices. The phone stays paired, its keyboard can reconnect without an approval prompt, and incoming calls and media audio connections are rejected so sound can stay on the phone. Other devices keep their normal Blueman behavior.

## Commands

Run as your desktop user, without `sudo`:

```bash
cd /data/mst/scm/btkeyboardonly

# List every paired device and its policy.
./btkeyboardonly list
./btkeyboardonly list --json

# Approve HID automatically and reject other service requests for the Redmi.
./btkeyboardonly enable 88:2F:92:4D:24:ED

# Remove this policy and restore the device's saved trust/block settings.
./btkeyboardonly reset 88:2F:92:4D:24:ED

# Restore every configured device.
./btkeyboardonly reset --all
```

`enable` accepts a Bluetooth address shown by `list`. It supports multiple selected devices. Repeating it preserves the original settings saved on the first enable. A currently connected device is disconnected once to clear connections accepted before the policy was applied; reconnect from its keyboard app afterward.

`list` shows `keyboard-only` when a device is configured, the agent service is active, Blueman's competing authorization agent is disabled, and whole-device trust/blocking are off. `configured/inactive` means the policy needs attention; rerun `enable` and inspect the service log. `default` means this tool has no policy for that device. `--json` also includes the blocked flag.

`reset` restores the settings saved before enabling, including whole-device trust if it was previously on. It does not unpair, remove Bluetooth keys, or initiate a connection. Resetting the last device also stops the agent, removes its startup entries, and restores Blueman's original AuthAgent selection. Other Blueman plugin selections are preserved. A device that has since been unpaired can still have its saved policy removed.

## Requirements

Linux with BlueZ, Blueman, Python 3, PyGObject, GTK 3, and a systemd user session. The current Fedora/XFCE setup already provides these. Pair the device first and run `enable` from a graphical desktop session. No pip packages or root installation are needed.

## How it works

BlueZ's `Trusted` property applies to the entire device. For selected devices this tool keeps that property off and registers a default BlueZ authorization agent instead. The agent automatically accepts service UUID `00001124-0000-1000-8000-00805f9b34fb` only while the device is paired and bonded, and rejects every other service request. Other devices retain Blueman's usual pairing and service dialogs.

This acts on **connection/service authorization**, not pairing. HID combines keyboard and mouse functionality; Bluetooth does not offer a keyboard-only switch within that profile. BLE-only HID devices are not supported and `enable` refuses them. This agent handles incoming requests that BlueZ submits for authorization; it does not intercept locally initiated profile connections from another application. Avoid explicitly connecting non-HID profiles from the computer. With media/call connections rejected, the phone should keep sound on its own output; verify that on the phone.

The source files are:

- `btkeyboardonly`: executable command entry point.
- `core.py`: CLI, state handling, installation, and shared policy functions.
- `agent.py`: the Bluetooth authorization agent, retaining Blueman's dialogs for other devices.

`enable` installs copies of `agent.py` and `core.py` into `~/.local/share/btkeyboardonly/`. Moving this checkout afterward does not affect startup. Rerun `enable` to install source updates.

## Configuration location

Configuration is saved in `~/.config/btkeyboardonly/state.json`, which is `/home/mst/.config/btkeyboardonly/state.json` on this machine. It records the selected devices, each device's original trust/block settings for `reset`, and the original Blueman AuthAgent selection. Keep this file to preserve those reset settings.

If `XDG_CONFIG_HOME` is set, the configuration path is `$XDG_CONFIG_HOME/btkeyboardonly/state.json` instead.

The service is `~/.config/systemd/user/btkeyboardonly.service`; desktop login starts it through `~/.config/autostart/btkeyboardonly.desktop`. Installed agent files live in `~/.local/share/btkeyboardonly/`. The tool honors `XDG_CONFIG_HOME` and `XDG_DATA_HOME` when set. Keep those consistent between the command and desktop session.

```bash
systemctl --user status btkeyboardonly.service
journalctl --user -u btkeyboardonly.service -f
python3 -m unittest discover -s tests -v
```

For this machine, the tool adopts the earlier hardcoded Redmi policy on first enable, removes its old service/autostart entries, and remembers that the Redmi was originally trusted. The earlier source under `~/.local/share/bluetooth-hid-policy/` is retained as a backup.
