# Ask PackageKit (Discover's backend) to refresh its cache, so Discover and its tray notifier see the new state.
import sys
from gi.repository import Gio, GLib
bus = Gio.bus_get_sync(Gio.BusType.SYSTEM)
tx = bus.call_sync("org.freedesktop.PackageKit", "/org/freedesktop/PackageKit", "org.freedesktop.PackageKit",
                   "CreateTransaction", None, GLib.VariantType("(o)"), 0, -1, None).unpack()[0]
loop = GLib.MainLoop(); result = {}
def on_signal(conn, sender, path, iface, name, params):
    if name == "Finished":
        result["exit"] = params.unpack()[0]; loop.quit()
    elif name == "ErrorCode":
        result["error"] = params.unpack()[1]
bus.signal_subscribe("org.freedesktop.PackageKit", "org.freedesktop.PackageKit.Transaction", None, tx, None, 0, on_signal)
bus.call_sync("org.freedesktop.PackageKit", tx, "org.freedesktop.PackageKit.Transaction", "RefreshCache",
              GLib.Variant("(b)", (False,)), None, 0, -1, None)
GLib.timeout_add_seconds(180, loop.quit)
loop.run()
print("PackageKit refresh:", "ok" if result.get("exit") == 1 else result)   # 1 = PK_EXIT_ENUM_SUCCESS
sys.exit(0 if result.get("exit") == 1 else 1)
