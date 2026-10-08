#!/usr/bin/python3
"""Per-device Bluetooth HID authorization for BlueZ and Blueman."""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile

from gi.repository import Gio, GLib

HID = "00001124-0000-1000-8000-00805f9b34fb"
AGENT_PATH = "/org/bluez/agent/btkeyboardonly"
SERVICE = "btkeyboardonly.service"
CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
DATA_HOME = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
STATE_DIR = CONFIG_HOME / "btkeyboardonly"
STATE_FILE = STATE_DIR / "state.json"
UNIT = CONFIG_HOME / "systemd/user" / SERVICE
AUTOSTART = CONFIG_HOME / "autostart/btkeyboardonly.desktop"
INSTALLED_AGENT = DATA_HOME / "btkeyboardonly/agent.py"


def read_state():
    if not STATE_FILE.exists():
        return {"version": 1, "devices": {}}
    state = json.loads(STATE_FILE.read_text())
    if state.get("version") != 1:
        raise ValueError("Unsupported state version")
    return state


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_state(state):
    atomic_write(STATE_FILE, json.dumps(state, indent=2) + "\n")


@contextlib.contextmanager
def locked():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with (STATE_DIR / "lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def systemctl(*args, check=True):
    return subprocess.run(["systemctl", "--user", *args], check=check,
                          text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def bluez_call(path, interface, method, parameters=None):
    bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
    return bus.call_sync("org.bluez", path, interface, method, parameters,
                         None, Gio.DBusCallFlags.NONE, 10000, None)


def paired_devices():
    objects = bluez_call("/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects").unpack()[0]
    return [(path, interfaces["org.bluez.Device1"]) for path, interfaces in objects.items()
            if interfaces.get("org.bluez.Device1", {}).get("Paired")]


def set_property(path, key, value):
    bluez_call(path, "org.freedesktop.DBus.Properties", "Set",
               GLib.Variant("(ssv)", ("org.bluez.Device1", key, GLib.Variant("b", value))))


def normalize_address(value):
    value = value.upper().replace("-", ":")
    if not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", value):
        raise ValueError("Use a Bluetooth address from the list command")
    return value


def select_device(address):
    matches = [(p, d) for p, d in paired_devices() if d["Address"].upper() == address]
    if len(matches) != 1:
        raise ValueError(f"Expected one paired device for {address}; found {len(matches)}")
    return matches[0]


def decision(state, address, uuid, paired, bonded):
    """None delegates to Blueman; False rejects; True authorizes HID."""
    if address.upper() not in state["devices"]:
        return None
    return uuid.lower() == HID and paired and bonded


def authagent_entries(entries):
    return [entry for entry in entries if entry.lstrip("!") == "AuthAgent"]


def restore_authagent(settings, originals):
    current = settings.get_strv("plugin-list")
    settings.set_strv("plugin-list", [e for e in current if e.lstrip("!") != "AuthAgent"] + originals)
    Gio.Settings.sync()


def legacy_setup():
    old_unit = CONFIG_HOME / "systemd/user/bluetooth-hid-policy.service"
    old_start = CONFIG_HOME / "autostart/bluetooth-hid-policy.desktop"
    original = DATA_HOME / "bluetooth-hid-policy/original-plugin-list"
    # Only adopt the exact earlier setup, not an unrelated service with that name.
    if old_unit.exists() and "bluetooth-hid-policy/agent.py" in old_unit.read_text() and original.exists():
        entries = GLib.Variant.parse(GLib.VariantType("as"), original.read_text(), None, None).unpack()
        return old_unit, old_start, entries
    return None


def install_agent():
    # Install a copy so moving the source checkout does not break login startup.
    root = Path(__file__).resolve().parent
    atomic_write(INSTALLED_AGENT, (root / "agent.py").read_text())
    atomic_write(INSTALLED_AGENT.parent / "core.py", (root / "core.py").read_text())
    quoted = str(INSTALLED_AGENT).replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    atomic_write(UNIT, f'''[Unit]
Description=Per-device Bluetooth keyboard-only authorization

[Service]
Type=notify
NotifyAccess=main
ExecStart=/usr/bin/python3 "{quoted}"
Restart=on-failure
RestartSec=3
TimeoutStartSec=20
''')
    atomic_write(AUTOSTART, '''[Desktop Entry]
Type=Application
Name=Bluetooth keyboard-only authorization
Exec=systemctl --user start btkeyboardonly.service
Terminal=false
''')
    systemctl("daemon-reload")


def enable_device(address):
    path, device = select_device(address)
    if HID not in [uuid.lower() for uuid in device.get("UUIDs", [])]:
        raise ValueError("Device does not advertise classic Bluetooth HID; BLE-only HID is not supported")
    if not device.get("Bonded"):
        raise ValueError("Device must already be paired and bonded")
    state = read_state()
    previous = json.loads(json.dumps(state))
    settings = Gio.Settings.new("org.blueman.general")
    plugins_before = settings.get_strv("plugin-list")
    legacy = legacy_setup()
    if not state["devices"]:
        state["authagent_original"] = authagent_entries(legacy[2] if legacy else plugins_before)
    if address not in state["devices"]:
        state["devices"][address] = {
            "name": device.get("Alias", device.get("Name", address)),
            "original_trusted": bool(device.get("Trusted")),
            "original_blocked": bool(device.get("Blocked")),
        }
        if legacy and address == "88:2F:92:4D:24:ED":
            state["devices"][address]["original_trusted"] = True
    save_state(state)
    try:
        install_agent()
        systemctl("restart", SERVICE)
        # The custom agent is READY before disabling Blueman's competing agent.
        restore_authagent(settings, ["!AuthAgent"])
        set_property(path, "Trusted", False)
        set_property(path, "Blocked", False)
    except Exception:
        save_state(previous)
        restore_authagent(settings, authagent_entries(plugins_before))
        if previous["devices"]:
            systemctl("restart", SERVICE, check=False)
        else:
            systemctl("stop", SERVICE, check=False)
            AUTOSTART.unlink(missing_ok=True)
            UNIT.unlink(missing_ok=True)
            systemctl("daemon-reload", check=False)
        set_property(path, "Trusted", bool(device.get("Trusted")))
        set_property(path, "Blocked", bool(device.get("Blocked")))
        raise
    if legacy:
        systemctl("stop", "bluetooth-hid-policy.service", check=False)
        legacy[0].unlink()
        legacy[1].unlink(missing_ok=True)
        systemctl("daemon-reload")
    if device.get("Connected"):
        bluez_call(path, "org.bluez.Device1", "Disconnect")
    print(f"{address}: keyboard-only enabled. Reconnect from the device's keyboard app.")


def reset_devices(address=None):
    state = read_state()
    if not state["devices"] and address is None:
        print("No keyboard-only devices configured.")
        return
    addresses = [address] if address else list(state["devices"])
    for item in addresses:
        if item not in state["devices"]:
            raise ValueError(f"{item} is not configured for keyboard-only authorization")
    paired = paired_devices()
    targets = []
    for item in addresses:
        matches = [path for path, device in paired if device["Address"].upper() == item]
        if len(matches) > 1:
            raise ValueError(f"Multiple paired devices for {item}; reset is ambiguous")
        targets.append((item, matches[0] if matches else None, state["devices"][item]))
    for item, path, saved in targets:
        del state["devices"][item]
        save_state(state)  # Stop enforcing before restoring whole-device trust.
        try:
            if path:
                set_property(path, "Trusted", saved["original_trusted"])
                set_property(path, "Blocked", saved["original_blocked"])
        except Exception:
            state["devices"][item] = saved
            save_state(state)
            raise
        print(f"{item}: restored original trust/block settings; pairing preserved." if path
              else f"{item}: policy removed; device is no longer paired.")
    if not state["devices"]:
        systemctl("stop", SERVICE, check=False)
        restore_authagent(Gio.Settings.new("org.blueman.general"), state.get("authagent_original", []))
        AUTOSTART.unlink(missing_ok=True)
        UNIT.unlink(missing_ok=True)
        systemctl("daemon-reload")
    else:
        systemctl("restart", SERVICE)


def list_devices(as_json=False):
    state = read_state()
    active = systemctl("is-active", SERVICE, check=False).returncode == 0
    auth_disabled = "!AuthAgent" in Gio.Settings.new("org.blueman.general").get_strv("plugin-list")
    rows = []
    for path, device in paired_devices():
        address = device["Address"].upper()
        configured = address in state["devices"]
        mode = "default"
        if configured:
            mode = "keyboard-only" if active and auth_disabled and not device.get("Trusted") and not device.get("Blocked") else "configured/inactive"
        rows.append(dict(address=address, name=device.get("Alias", address), mode=mode,
                         trusted=bool(device.get("Trusted")), blocked=bool(device.get("Blocked")),
                         connected=bool(device.get("Connected"))))
    if as_json:
        print(json.dumps(rows, indent=2))
    else:
        print(f'{"ADDRESS":17}  {"MODE":20}  {"TRUSTED":7}  {"CONNECTED":9}  NAME')
        for row in rows:
            print(f'{row["address"]:17}  {row["mode"]:20}  {str(row["trusted"]):7}  {str(row["connected"]):9}  {row["name"]}')
        if not rows:
            print("No paired devices.")


def notify(message):
    address = os.environ.get("NOTIFY_SOCKET")
    if address:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect("\0" + address[1:] if address.startswith("@") else address)
            sock.sendall(message.encode())



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="List paired devices and their policy")
    listing.add_argument("--json", action="store_true")
    enabling = commands.add_parser("enable", help="Automatically approve HID and deny other service requests")
    enabling.add_argument("device", type=normalize_address, metavar="ADDRESS")
    resetting = commands.add_parser("reset", help="Restore saved trust/block settings and ordinary Blueman authorization")
    group = resetting.add_mutually_exclusive_group(required=True)
    group.add_argument("device", nargs="?", type=normalize_address, metavar="ADDRESS")
    group.add_argument("--all", action="store_true")
    args = parser.parse_args()
    if args.command == "list":
        list_devices(args.json)
    else:
        with locked():
            if args.command == "enable":
                enable_device(args.device)
            else:
                reset_devices(args.device)


def cli():
    try:
        main()
    except (ValueError, RuntimeError, OSError, GLib.Error, subprocess.CalledProcessError) as error:
        print(f"btkeyboardonly: {error}", file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            print(error.stderr.strip(), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    cli()
