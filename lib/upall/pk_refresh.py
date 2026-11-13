# Ask PackageKit (Discover's backend) to refresh its cache, so Discover and its tray notifier see the new state.
#
# Output contract: the full PackageKit error goes first (it ends up in packagekit.log), and the LAST line is a short
# human summary, which is the one bin/upall shows. Exit codes: 0 ok, 1 failed, 2 timed out, 3 some sources unreachable.
import re
import sys

EXIT_SUCCESS = 1   # PK_EXIT_ENUM_SUCCESS
CONNECTION = re.compile(r"is not \(yet\) available|Could not connect|Cannot initiate the connection|Connection refused"
                        r"|Network is unreachable|Temporary failure resolving|Could not resolve|timed out", re.I)


def summarize(result):
    """(exit code, one-line summary) for a PackageKit result dict {"exit": n, "error": text}."""
    if result.get("exit") == EXIT_SUCCESS:
        return 0, "PackageKit refresh: ok"
    if not result:
        return 2, "PackageKit refresh: timed out (no answer from PackageKit)"
    # apt's "E: <url> <suite> InRelease is not (yet) available (...)" / "Failed to fetch <url> ..." lines:
    # the repo URL tells which sources are unreachable.
    err = result.get("error", "")
    repos = {}
    for m in re.finditer(r"(?:E: |Failed to fetch )(https?://([^/\s]+)/\S*)", err):
        url = re.sub(r"/dists/.*", "", m.group(1))
        repos.setdefault(m.group(2), set()).add(url)
    if repos:
        n = sum(len(v) for v in repos.values())
        hosts = sorted(repos, key=lambda h: -len(repos[h]))
        shown = ", ".join(hosts[:3]) + (f" +{len(hosts) - 3} more" if len(hosts) > 3 else "")
        plural = "s" * (n != 1)
        # Only a connection problem clears by itself; a 404/403/bad hash on a repo does not.
        if CONNECTION.search(err):
            return 3, f"PackageKit refresh: {n} source{plural} unreachable on {shown}"
        return 1, f"PackageKit refresh: failed on {n} source{plural} ({shown})"
    first = err.strip().splitlines()[0][:160] if err.strip() else f"exit {result.get('exit')}"
    return 1, f"PackageKit refresh: failed ({first})"


def main():
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
    code, line = summarize(result)
    if result.get("error"):
        print(result["error"])          # full detail for the log
    print(line)                         # last line: what upall shows
    sys.exit(code)


if __name__ == "__main__":
    main()
