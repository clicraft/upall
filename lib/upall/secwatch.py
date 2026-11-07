#!/usr/bin/env python3
"""upall-secwatch: check for pending Ubuntu security updates after login and after waking up.

Runs as a systemd user service in the Plasma session (~/.config/systemd/user/upall-secwatch.service).
  - after login: waits LOGIN_DELAY, then checks
  - after resume from sleep/hibernation: logind's PrepareForSleep(false) signal, waits RESUME_DELAY, then checks
  - no check if the last one was less than MIN_INTERVAL ago (several short naps = one check)
The check: wait for the network, refresh apt lists (passwordless `sudo -n apt-get update`), list upgradable
packages from a *-security pocket. If there are any and AUTO_APPLY is on (default; AUTO_APPLY=0 in
~/.config/upall/secwatch.conf turns it off), `upall --system` installs them unattended, with all of upall's
safety rules; the dpkg.log watcher below pushes "applied". If they are still pending after that (or AUTO_APPLY=0),
a KDE notification: "Update now" opens `upall --tui`
in a new WezTerm window, "Details" opens Ubuntu's CVE pages for those packages. The same set is not
announced again within RENOTIFY. Only security updates: Discover's own notifier covers the rest.

  secwatch.py            run as the service (listen for login/resume)
  secwatch.py --once     one check now, respecting MIN_INTERVAL
  secwatch.py --force    one check now, ignoring MIN_INTERVAL and RENOTIFY
  secwatch.py --test-push  send a test message to the phone
  secwatch.py --push TITLE MESSAGE [TAGS]   send any message
  secwatch.py --scan-dpkg  report security updates dpkg installed since the last scan, now

Applied updates: whoever installs them (Discover, apt, nala, upall, offline updates at boot), dpkg logs them in
/var/log/dpkg.log; this service reads new lines (poll every 30 s, report after 30 s of quiet = one push per run)
and pushes "N security updates applied" for versions that come from a *-security pocket.
Nothing here ever reboots the PC: "Reboot needed" is only a message.

Phone: the same alert is pushed through ntfy (https://ntfy.sh) to the topic in ~/.config/upall/ntfy.conf.
The topic name is the only secret (anyone who knows it can read/post): kept out of git.
"""
import json, os, re, socket, subprocess, sys, threading, time, urllib.request

from gi.repository import Gio, GLib

LOGIN_DELAY = 45          # s; give the network time to come up after login (~30 s on a typical desktop)
RESUME_DELAY = 20         # s
MIN_INTERVAL = 3 * 3600   # s between checks
RENOTIFY = 20 * 3600      # s before announcing the same set again
STATE = os.path.expanduser("~/.local/state/upall/secwatch.json")
NTFY_CONF = os.path.expanduser("~/.config/upall/ntfy.conf")   # NTFY_URL=https://ntfy.sh/<random topic>; mode 600
CONF = os.path.expanduser("~/.config/upall/secwatch.conf")    # AUTO_APPLY=0 turns automatic installing off
UPALL = os.path.expanduser("~/.local/bin/upall")
RUNLOG_DIR = os.path.expanduser("~/.local/state/upall")
DPKG_LOG = os.environ.get("SECWATCH_DPKG_LOG", "/var/log/dpkg.log")   # override for tests
DPKG_POLL = 30            # s between size checks of dpkg.log
DPKG_QUIET = 30           # s without new dpkg.log lines before reporting (one push per apt/Discover run)
DPKG_LOCK = threading.Lock()
ENV_C = {**os.environ, "LC_ALL": "C.UTF-8"}
CHECK_LOCK = threading.Lock()
HOST = socket.gethostname().split(".")[0]   # push titles start with it: "myhost: 2 security updates applied"


def log(msg):
    print(msg, flush=True)        # goes to the journal: journalctl --user -u upall-secwatch


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(st):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w") as f:
        json.dump(st, f, indent=1)


def security_updates():
    """[(package, new_version)] upgradable from a *-security pocket."""
    out = subprocess.run(["apt", "list", "--upgradable"], capture_output=True, text=True, env=ENV_C).stdout
    found = []
    for line in out.splitlines():
        m = re.match(r"(\S+?)/(\S+) (\S+) ", line)
        if m and "-security" in m.group(2):
            found.append((m.group(1), m.group(3)))
    return found


def source_packages(pkgs):
    out = subprocess.run(["dpkg-query", "-W", "-f=${source:Package}\n", *pkgs], capture_output=True,
                         text=True, env=ENV_C).stdout
    return sorted(set(filter(None, out.split())))


def ntfy_url():
    try:
        for line in open(NTFY_CONF):
            if line.startswith("NTFY_URL="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return None


def push(title, message, click=None, priority="high", tags="rotating_light"):
    """Phone push through ntfy (topic in NTFY_CONF). Silent no-op without a config; never raises."""
    url = ntfy_url()
    if not url:
        return
    headers = {"Title": title.encode(), "Priority": priority, "Tags": tags}
    if click:
        headers["Click"] = click
    try:
        req = urllib.request.Request(url, data=message.encode(), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            log(f"ntfy push sent ({r.status})")
    except Exception as e:  # network down, ntfy.sh unreachable: the desktop notification still shows
        log(f"ntfy push failed: {e}")


# ------------------------------------------------------------ applied security updates (any installer)
# dpkg writes /var/log/dpkg.log for every install: Discover/PackageKit, apt, nala, upall, offline updates at
# boot. Reading it needs no root and no apt hook. "upgrade pkg:arch OLD NEW" / "install pkg:arch <none> NEW"
# lines are the real version changes (status lines also appear for reconfigure/triggers).
def dpkg_new_versions(st):
    """(pkg, new_version) installed since the saved offset. First run: baseline at end of file, nothing reported."""
    try:
        f = open(DPKG_LOG, errors="replace")
    except OSError:
        return []
    with f:
        ino, size = os.fstat(f.fileno()).st_ino, os.fstat(f.fileno()).st_size
        if "dpkg_ino" not in st:
            st["dpkg_ino"], st["dpkg_off"] = ino, size
            return []
        off = st["dpkg_off"] if ino == st["dpkg_ino"] and size >= st["dpkg_off"] else 0   # rotated: from 0
        f.seek(off)
        text = f.read()
        st["dpkg_ino"], st["dpkg_off"] = ino, off + len(text.encode())
    found = {}
    for m in re.finditer(r"^\S+ \S+ (?:upgrade|install) (\S+?)(?::\S+)? \S+ (\S+)$", text, re.M):
        found[m.group(1)] = m.group(2)
    return sorted(found.items())


def from_security_pocket(pkg, ver):
    """True if any apt source offering this exact version is a *-security pocket (Ubuntu security fix)."""
    out = subprocess.run(["apt-cache", "madison", pkg], capture_output=True, text=True, env=ENV_C).stdout
    for line in out.splitlines():
        parts = [x.strip() for x in line.split("|")]
        if len(parts) >= 3 and parts[1] == ver and re.search(r"\s\S+-security/", parts[2]):
            return True
    return False


def report_applied():
    """Push "N security updates applied" for security versions dpkg installed since last time."""
    if not DPKG_LOCK.acquire(blocking=False):
        return
    try:
        st = load_state()
        new = dpkg_new_versions(st)
        save_state(st)
        if not new:
            return
        sec = [(p, v) for p, v in new if from_security_pocket(p, v)]
        log(f"dpkg: {len(new)} package version(s) installed, {len(sec)} security: {' '.join(p for p, _ in sec)}")
        if not sec:
            return
        pending = [p for p, _ in security_updates()]
        body = " ".join(p for p, _ in sec)
        if pending:
            body += "\nStill pending: " + " ".join(pending)
        if os.path.exists("/var/run/reboot-required"):
            body += "\nReboot needed (not done automatically)"
        n = len(sec)
        push(f"{HOST}: {n} security update{'s' if n != 1 else ''} applied", body,
             priority="default", tags="white_check_mark,lock")
    finally:
        DPKG_LOCK.release()


def watch_dpkg():
    """Poll dpkg.log size; report once it has been quiet for DPKG_QUIET seconds after a change."""
    state = {"size": None, "changed": None}

    def tick():
        try:
            size = os.stat(DPKG_LOG).st_size
        except OSError:
            return True
        now = time.monotonic()
        if state["size"] is not None and size != state["size"]:
            state["changed"] = now
        state["size"] = size
        if state["changed"] and now - state["changed"] >= DPKG_QUIET:
            state["changed"] = None
            threading.Thread(target=report_applied, daemon=True).start()
        return True

    GLib.timeout_add_seconds(DPKG_POLL, tick)


def auto_apply_enabled():
    try:
        for line in open(CONF):
            if line.strip().startswith("AUTO_APPLY="):
                return line.split("=", 1)[1].strip() not in ("0", "no", "false")
    except OSError:
        pass
    return True


def desktop(title, body, urgency="normal", icon="security-high"):
    """Non-blocking desktop notification (no buttons)."""
    subprocess.Popen(["notify-send", "-a", "upall", "-i", icon, "-u", urgency, title, body],
                     start_new_session=True)


def auto_apply(updates):
    """Run `upall --system` unattended. Its own safety rules apply: with nobody to answer, a risky apt plan
    (removals, kernel vs /boot) or a failed Timeshift snapshot means apt is skipped. The phone's "applied"
    push comes from the dpkg.log watcher (report_applied). Never reboots. Returns what is still pending."""
    names = " ".join(p for p, _ in updates)
    desktop(f"Installing {len(updates)} security update{'s' if len(updates) != 1 else ''}…", names,
            urgency="low", icon="system-software-update")
    os.makedirs(RUNLOG_DIR, exist_ok=True)
    runlog = os.path.join(RUNLOG_DIR, f"secwatch-upall-{time.strftime('%Y%m%d-%H%M%S')}.log")
    log(f"running upall --system (log {runlog})")
    with open(runlog, "w") as f:
        rc = subprocess.run([UPALL, "--system"], stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT,
                            env={**os.environ, "TERM": "dumb"}).returncode
    remaining = security_updates()
    log(f"upall --system exit {rc}; {len(remaining)} security update(s) still pending")
    if not remaining:
        reboot = os.path.exists("/var/run/reboot-required")
        desktop(f"{len(updates)} security update{'s' if len(updates) != 1 else ''} installed",
                names + ("\nReboot needed: do it when it suits you" if reboot else ""), icon="security-high")
    return remaining, runlog


def notify(updates, title=None):
    names = [p for p, _ in updates]
    shown = ", ".join(names[:6]) + (f" +{len(names) - 6}" if len(names) > 6 else "")
    title = title or f"{len(names)} security update{'s' if len(names) != 1 else ''} pending"
    srcs = source_packages(names)
    push(f"{HOST}: {title}", ", ".join(names) + "\nRun: upall --tui",
         click=f"https://ubuntu.com/security/cves?package={srcs[0]}" if srcs else None)
    r = subprocess.run(["notify-send", "-a", "upall", "-i", "security-medium", "-u", "critical",
                        "-A", "update=Update now", "-A", "details=Details", title, shown],
                       capture_output=True, text=True)
    action = r.stdout.strip()
    log(f"notification closed, action={action or 'none'}")
    if action == "update":
        subprocess.Popen(["wezterm", "start", "--always-new-process", "--", "bash", "-c",
                          "upall --tui; echo; read -rp 'Press Enter to close this window '"],
                         start_new_session=True)
    elif action == "details":
        for src in srcs[:5]:
            subprocess.Popen(["xdg-open", f"https://ubuntu.com/security/cves?package={src}"],
                             start_new_session=True)


def check(reason, force=False):
    if not CHECK_LOCK.acquire(blocking=False):
        log(f"{reason}: a check is already running")
        return
    try:
        st = load_state()
        now = time.time()
        if not force and now - st.get("last_check", 0) < MIN_INTERVAL:
            log(f"{reason}: last check {int((now - st['last_check']) / 60)} min ago, skipping")
            return
        log(f"{reason}: waiting for the network")
        subprocess.run(["nm-online", "-q", "-t", "120"])
        r = subprocess.run(["sudo", "-n", "apt-get", "update", "-qq", "-o", "DPkg::Lock::Timeout=120"],
                           capture_output=True, text=True, env=ENV_C)
        if r.returncode != 0:
            log(f"apt-get update failed ({r.returncode}): {(r.stderr or r.stdout).strip()[-200:]}; using cached lists")
        updates = security_updates()
        st["last_check"] = now
        log(f"{reason}: {len(updates)} security update(s): {' '.join(p for p, _ in updates)}")
        title = None
        if updates and auto_apply_enabled():
            save_state(st)
            updates, runlog = auto_apply(updates)
            if updates:   # not installed: risky plan with nobody to confirm, no snapshot, or apt failed
                title = (f"{len(updates)} security updates need you" if len(updates) != 1
                         else "1 security update needs you")
                force = True      # always tell: an automatic attempt just failed
        key = sorted(f"{p}={v}" for p, v in updates)
        if updates and (force or key != st.get("last_notified_set") or now - st.get("last_notified", 0) >= RENOTIFY):
            st["last_notified_set"], st["last_notified"] = key, now
            save_state(st)
            notify(updates, title)    # blocks until the notification is closed or answered
        else:
            save_state(st)
    finally:
        CHECK_LOCK.release()


def later(delay, reason):
    def run():
        threading.Thread(target=check, args=(reason,), daemon=True).start()
        return False
    GLib.timeout_add_seconds(delay, run)


def main():
    if "--test-push" in sys.argv:
        push(f"{HOST}: test from upall-secwatch", "If you can read this, security alerts will reach this phone.",
             priority="default", tags="white_check_mark")
        return
    if len(sys.argv) >= 4 and sys.argv[1] == "--push":
        push(sys.argv[2], sys.argv[3], priority="default", tags=sys.argv[4] if len(sys.argv) > 4 else "white_check_mark")
        return
    if "--scan-dpkg" in sys.argv:
        report_applied()
        return
    if "--once" in sys.argv or "--force" in sys.argv:
        check("manual", force="--force" in sys.argv)
        return
    bus = Gio.bus_get_sync(Gio.BusType.SYSTEM)

    def on_sleep(conn, sender, path, iface, signal, params):
        going_to_sleep = params.unpack()[0]
        if not going_to_sleep:
            log("resumed from sleep/hibernation")
            later(RESUME_DELAY, "resume")

    bus.signal_subscribe("org.freedesktop.login1", "org.freedesktop.login1.Manager", "PrepareForSleep",
                         "/org/freedesktop/login1", None, Gio.DBusSignalFlags.NONE, on_sleep)
    log("started; first check after login; watching dpkg.log for applied security updates")
    threading.Thread(target=report_applied, daemon=True).start()   # catch installs while logged out (boot)
    watch_dpkg()
    later(LOGIN_DELAY, "login")
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
