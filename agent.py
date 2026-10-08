#!/usr/bin/python3
"""BlueZ authorization agent: HID only for configured devices."""
import logging
import signal
from gi.repository import Gio, GLib
from core import AGENT_PATH, bluez_call, decision, notify, paired_devices, read_state, set_property


def run_agent():
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk, GLibUnix
    from blueman.main.applet.BluezAgent import BluezAgent, BluezErrorRejected
    from blueman.bluez.Device import Device

    class KeyboardAgent(BluezAgent):
        def _on_authorize_service(self, path, uuid, ok, err):
            device = Device(obj_path=path)
            address = device["Address"].upper()
            allowed = decision(read_state(), address, uuid, device["Paired"], device["Bonded"])
            if allowed is None:
                return super()._on_authorize_service(path, uuid, ok, err)
            logging.info("%s %s service %s", "ALLOW" if allowed else "DENY", address, uuid)
            if allowed:
                ok()
            else:
                err(BluezErrorRejected("Only bonded HID connections are authorized for this device"))

    if not Gtk.init_check()[0]:
        raise RuntimeError("A graphical login is required for the Blueman pairing dialogs")
    agent = KeyboardAgent()
    agent._path = AGENT_PATH
    agent.register()
    bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
    loop = GLib.MainLoop()

    def enforce(path, properties):
        if properties.get("Address", "").upper() in read_state()["devices"]:
            if properties.get("Trusted"):
                set_property(path, "Trusted", False)

    def appeared(connection, name, owner):
        try:
            if agent._regid is None:
                agent.register()
            bluez_call("/org/bluez", "org.bluez.AgentManager1", "RegisterAgent",
                       GLib.Variant("(os)", (AGENT_PATH, "KeyboardDisplay")))
            bluez_call("/org/bluez", "org.bluez.AgentManager1", "RequestDefaultAgent",
                       GLib.Variant("(o)", (AGENT_PATH,)))
            for path, properties in paired_devices():
                enforce(path, properties)
            logging.info("READY: HID-only service authorization active")
            notify("READY=1\nSTATUS=HID-only service authorization active")
        except Exception:
            logging.exception("Cannot activate Bluetooth authorization")
            raise SystemExit(1)

    def changed(connection, sender, path, interface, member, parameters):
        try:
            if member == "InterfacesAdded":
                added_path, interfaces = parameters.unpack()
                enforce(added_path, interfaces.get("org.bluez.Device1", {}))
            else:
                iface, properties, invalidated = parameters.unpack()
                if iface == "org.bluez.Device1" and properties.get("Trusted"):
                    device = Device(obj_path=path)
                    enforce(path, {"Address": device["Address"], "Trusted": True})
        except Exception:
            logging.exception("Unable to enforce device authorization")
            raise SystemExit(1)

    bus.signal_subscribe("org.bluez", "org.freedesktop.DBus.Properties", "PropertiesChanged",
                         None, None, Gio.DBusSignalFlags.NONE, changed)
    bus.signal_subscribe("org.bluez", "org.freedesktop.DBus.ObjectManager", "InterfacesAdded",
                         None, None, Gio.DBusSignalFlags.NONE, changed)
    Gio.bus_watch_name_on_connection(bus, "org.bluez", Gio.BusNameWatcherFlags.NONE, appeared,
                                     lambda *args: notify("STATUS=Waiting for BlueZ"))
    for sig in (signal.SIGTERM, signal.SIGINT):
        GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, sig, lambda: (loop.quit(), False)[1])
    loop.run()



if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    run_agent()
