#!/usr/bin/env python3
"""upall --tui: live view of apt (nala), Flatpak, Snap and topgrade updates.

Started by ~/.local/bin/upall after `sudo -v` and `apt-get update`. Each source reports through the
most precise channel it has:
  apt      runs `nala upgrade`; progress = bytes of the planned .debs found in /var/cache/apt/archives
           plus the size of archives/partial (read through `upall-root apt-partial`, once a second), then dpkg lines in the log
  snap     starts `snap refresh --no-wait`, then reads per-task byte progress from the snapd REST API
  flatpak  runs the update in-process with libflatpak (gi), which reports bytes and status per operation
  tools    runs topgrade after the group, in parallel lanes if ~/.config/upall/tools-lanes.conf says so
           (tools_lanes.py); progress = steps seen in the lane logs / steps in a dry run
Package jobs run in their own process group, so Ctrl-C stops this view but not apt/dpkg or snapd.

Options: --run DIR (log dir), --no-tools, --after-apt snap,flatpak, --plan (print plan and exit), --demo
"""
import argparse, http.client, json, os, re, signal, socket, subprocess, sys, threading, time, warnings
from dataclasses import dataclass, field

from rich.console import Console, Group
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

ARCHIVES = "/var/cache/apt/archives"
ENV_C = {**os.environ, "LC_ALL": "C.UTF-8"}
# Every root command goes through upall-root (fixed actions only); upall passes the copy it checked.
ROOT = os.environ.get("UPALL_ROOT", "/usr/local/sbin/upall-root")
# sha256 of `upall-root plan`, the plan upall checked: upall-root upgrade refuses to run if the plan changed since.
PLAN = os.environ.get("UPALL_PLAN", "")


UNITS = ("B", "KiB", "MiB", "GiB", "TiB")   # IEC binary units, 1 KiB = 1024 B


def unit_for(n):
    """Index into UNITS for a byte count."""
    i, n = 0, float(n or 0)
    while n >= 1024 and i < len(UNITS) - 1:
        n /= 1024
        i += 1
    return i


def human(n, unit=None):
    """Bytes as '412.0 MiB' (B and KiB without decimals). unit forces an index into UNITS."""
    i = unit_for(n) if unit is None else unit
    v = float(n or 0) / 1024 ** i
    return f"{v:.0f} {UNITS[i]}" if i < 2 else f"{v:.1f} {UNITS[i]}"


def pair(done, total):
    """'412.0/601.0 MiB': both numbers in the unit of the total."""
    i = unit_for(total)
    return f"{human(done, i).split()[0]}/{human(total, i)}"


def clock(s):
    s = int(s)
    return f"{s // 60}m{s % 60:02d}s"


@dataclass
class Job:
    name: str
    state: str = "waiting"          # waiting, planning, downloading, installing, running, done, failed, skipped
    total: int = 0                  # bytes to download (0 = unknown)
    done: int = 0                   # bytes downloaded
    items_total: int = 0            # packages / refs / steps
    items_done: int = 0
    current: str = ""
    note: str = ""
    log: str = ""                  # the log to tail when it fails (default: <name>.log)
    speed: float = 0.0              # bytes/s, smoothed
    started: float = 0.0
    ended: float = 0.0
    rc: int | None = None
    _last: tuple = field(default=(0.0, 0))

    def set_bytes(self, done):
        now = time.monotonic()
        t0, b0 = self._last
        if t0 and now - t0 >= 0.4:
            inst = max(0, done - b0) / (now - t0)
            self.speed = inst if self.speed == 0 else 0.6 * self.speed + 0.4 * inst
            self._last = (now, done)
        elif not t0:
            self._last = (now, done)
        self.done = done

    def start(self, state="running"):
        self.state, self.started = state, time.monotonic()

    def finish(self, rc, note=""):
        self.rc, self.ended, self.speed = rc, time.monotonic(), 0.0
        self.state = "done" if rc == 0 else "failed"
        if note:
            self.note = note

    @property
    def fraction(self):
        if self.state in ("done", "skipped"):
            return 1.0
        if self.state == "downloading" and self.total:
            return min(1.0, self.done / self.total)
        if self.items_total:
            return min(1.0, self.items_done / self.items_total)
        return 0.0


EVENTS: list[str] = []
LOCK = threading.Lock()


def event(msg):
    with LOCK:
        EVENTS.append(f"{time.strftime('%H:%M:%S')}  {msg}")
        del EVENTS[:-8]


def popen(cmd, log, extra_env=None, detach_tty=False, **kw):
    """Package jobs get their own process group: terminal Ctrl-C must not reach dpkg/snapd.

    detach_tty: own session with no controlling terminal. Needed for topgrade: some of its steps
    (SDKMAN runs `bash -i -c ...`) grab the terminal, and from a background process group the
    kernel stops them (SIGTTOU/SIGTTIN, state T) forever. Not for sudo jobs: sudo's ticket is per tty.
    """
    grp = {"start_new_session": True} if detach_tty else {"process_group": 0}
    return subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                            env={**ENV_C, **(extra_env or {})}, **grp, **kw)


# ------------------------------------------------------------------------------------------ apt
def apt_plan():
    """Files nala will download (name -> size) and number of package actions."""
    uris = subprocess.run(["apt-get", "--print-uris", "-qq", "full-upgrade"], capture_output=True,
                          text=True, env=ENV_C).stdout
    files = {}
    for line in uris.splitlines():
        m = re.match(r"'([^']+)' (\S+) (\d+)", line)
        if m:
            files[m.group(2)] = int(m.group(3))
    sim = subprocess.run(["apt-get", "-s", "-o", "Debug::NoLocking=1", "full-upgrade"], capture_output=True,
                         text=True, env=ENV_C).stdout
    inst = [l.split()[1] for l in sim.splitlines() if l.startswith("Inst ")]
    return files, inst


def root_out(*action):
    r = subprocess.run(["sudo", "-n", ROOT, *action], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def run_apt(job, rundir):
    job.state = "planning"
    files, inst = apt_plan()
    job.total, job.items_total = sum(files.values()), len(inst)
    if not inst:
        job.state, job.note = "skipped", "up to date"
        event("apt: nothing to upgrade")
        return
    if not re.fullmatch(r"[0-9a-f]{64}", PLAN):
        job.finish(1, "no checked plan (UPALL_PLAN): start the TUI through upall --tui")
        event("apt: FAILED, no checked plan")
        return
    event(f"apt: {len(inst)} packages, {human(job.total)} to download")
    with open(f"{rundir}/apt.log", "w") as log:
        # upall-root upgrade = nala upgrade --full -y --no-update with upall's apt options, if the plan is unchanged
        p = popen(["sudo", "-n", ROOT, "upgrade", PLAN], log)
    job.start("downloading" if files else "installing")
    pending = dict(files)
    seen = set()
    while p.poll() is None:
        if job.state == "downloading":
            for f in [f for f in pending if os.path.exists(f"{ARCHIVES}/{f}")]:
                del pending[f]
            # one sudo call per second (partial/ is _apt-only); `ls -ln` gives names and sizes
            partial = {}
            for line in root_out("apt-partial").splitlines():
                parts = line.split()
                if len(parts) >= 9 and parts[-1].endswith(".deb") and parts[4].isdigit():
                    partial[parts[-1]] = int(parts[4])
            job.set_bytes(job.total - sum(pending.values()) + sum(partial.values()))
            job.items_done = len(files) - len(pending)
            in_partial = list(partial)
            job.current = (in_partial[0].split("_")[0] + (f" +{len(in_partial) - 1}" if len(in_partial) > 1 else "")
                           ) if in_partial else ""
            if not pending:
                job.state, job.items_done, job.speed = "installing", 0, 0.0
                event(f"apt: downloaded {human(job.total)}, installing")
        if job.state == "installing":
            try:
                text = open(f"{rundir}/apt.log", errors="replace").read()
            except OSError:
                text = ""
            ups = re.findall(r"Setting up:?\s+(\S+)", text)
            for name in ups:
                seen.add(name)
            job.items_done = min(len(seen), job.items_total)
            last = re.findall(r"(Unpacking|Setting up|Removing|Processing triggers for):?\s+(\S+)", text)
            if last:
                job.current = f"{last[-1][0].lower()} {last[-1][1]}"
        time.sleep(1.0)
    rc = p.wait()
    tail = open(f"{rundir}/apt.log", errors="replace").read()
    summary = ", ".join(re.findall(r"(?:Upgrade|Install|Auto-Remove|Remove)d? +\d+ Packages?", tail)[:3])
    job.current = ""
    job.finish(rc, summary or ("" if rc == 0 else "see apt.log"))
    event(f"apt: {'done' if rc == 0 else f'FAILED (exit {rc})'} {summary}")


# ----------------------------------------------------------------------------------------- snap
class SnapdConn(http.client.HTTPConnection):
    def __init__(self):
        super().__init__("localhost", timeout=10)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect("/run/snapd.socket")


def snapd(path):
    c = SnapdConn()
    try:
        c.request("GET", path)
        return json.loads(c.getresponse().read()).get("result")
    finally:
        c.close()


def snap_plan():
    res = snapd("/v2/find?select=refresh") or []
    if isinstance(res, dict):       # "no updates" comes back as an error object
        return []
    return [(s["name"], s.get("version", "?"), int(s.get("download-size") or 0)) for s in res]


def run_snap(job, rundir):
    job.state = "planning"
    plan = snap_plan()
    job.total, job.items_total = sum(s[2] for s in plan), len(plan)
    if not plan:
        job.state, job.note = "skipped", "up to date"
        event("snap: nothing to refresh")
        return
    event("snap: " + ", ".join(f"{n} {v}" for n, v, _ in plan))
    with open(f"{rundir}/snap.log", "w") as log:
        r = subprocess.run(["sudo", "-n", ROOT, "snap-refresh", "--no-wait"], env=ENV_C,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, process_group=0)
        log.write(r.stdout + r.stderr)
    change = r.stdout.strip()
    if r.returncode != 0 or not change.isdigit():
        job.finish(r.returncode or 1, (r.stderr or r.stdout).strip().splitlines()[-1][:70] if (r.stderr or r.stdout) else "")
        event(f"snap: FAILED to start: {job.note}")
        return
    job.start("downloading")
    while True:
        ch = snapd(f"/v2/changes/{change}") or {}
        tasks = ch.get("tasks", [])
        dl = [t for t in tasks if t.get("kind") == "download-snap"]
        job.set_bytes(sum(t.get("progress", {}).get("done", 0) for t in dl))
        if dl and all(t.get("status") in ("Done", "Undone", "Error") for t in dl) and job.state == "downloading":
            job.state, job.speed = "installing", 0.0
            event(f"snap: downloaded {human(job.done)}, installing")
        if job.state == "installing":
            job.items_total = len(tasks)
            job.items_done = sum(t.get("status") == "Done" for t in tasks)
        doing = [t for t in tasks if t.get("status") == "Doing"]
        job.current = doing[0].get("summary", "")[:60] if doing else ""
        if ch.get("ready"):
            ok = ch.get("status") == "Done"
            err = (ch.get("err") or "").splitlines()
            job.current = ""
            job.finish(0 if ok else 1, "" if ok else (err[-1] if err else ch.get("status", ""))[:70])
            with open(f"{rundir}/snap.log", "a") as log:
                log.write(json.dumps(ch, indent=1))
            event(f"snap: {'done' if ok else 'FAILED: ' + job.note}")
            return
        time.sleep(0.5)


# -------------------------------------------------------------------------------------- flatpak
def flatpak_installs():
    import gi
    gi.require_version("Flatpak", "1.0")
    from gi.repository import Flatpak
    out = []
    for inst in (Flatpak.Installation.new_system(None), Flatpak.Installation.new_user(None)):
        refs = inst.list_installed_refs_for_update(None)
        if refs:
            out.append((inst, refs))
    return Flatpak, out


def run_flatpak(job, rundir):
    job.state = "planning"
    try:
        Flatpak, installs = flatpak_installs()
    except Exception as e:  # bindings missing: plain CLI, no byte progress
        return run_flatpak_cli(job, rundir, str(e))
    refs = [r.get_name() for _, rs in installs for r in rs]
    if not refs:
        job.state, job.note = "skipped", "up to date"
        event("flatpak: nothing to update")
        return
    job.items_total = len(refs)
    event("flatpak: " + ", ".join(refs))
    log = open(f"{rundir}/flatpak.log", "w")
    job.start("downloading")
    base = 0           # bytes from finished operations
    errors = []

    for inst, rs in installs:
        t = Flatpak.Transaction.new_for_installation(inst, None)
        try:
            t.set_no_interaction(True)
        except AttributeError:
            pass
        for r in rs:
            t.add_update(r.format_ref(), None, None)

        def on_ready(tr):
            job.total += sum(op.get_download_size() for op in tr.get_operations())
            return True

        def on_new_op(tr, op, progress):
            ref = op.get_ref().split("/")[1]
            job.current = ref
            log.write(f"start {op.get_ref()}\n")
            progress.set_update_frequency(400)

            def changed(pr):
                job.set_bytes(base + pr.get_bytes_transferred())
                st = pr.get_status() or ""
                job.current = f"{ref}  {st[:40]}"
            progress.connect("changed", changed)

        def on_done(tr, op, commit, result):
            nonlocal base
            base = job.done
            job.items_done += 1
            log.write(f"done  {op.get_ref()} {commit}\n")
            event(f"flatpak: updated {op.get_ref().split('/')[1]}")

        def on_error(tr, op, err, details):
            errors.append(f"{op.get_ref()}: {err.message}")
            log.write(f"error {op.get_ref()}: {err.message}\n")
            return True  # continue with the other refs

        t.connect("ready", on_ready)
        t.connect("new-operation", on_new_op)
        t.connect("operation-done", on_done)
        t.connect("operation-error", on_error)
        try:
            # libflatpak 1.14 registers operation-done's `result` as an int while its GIR says flags: PyGObject then
            # warns "g_value_get_flags: assertion ... failed" once per operation, over the live display. We don't read it.
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", message=r"g_value_get_flags: assertion 'G_VALUE_HOLDS_FLAGS")
                t.run(None)
        except Exception as e:
            errors.append(str(e))
            log.write(f"run failed: {e}\n")
    log.close()
    job.current = ""
    job.finish(1 if errors else 0, errors[0][:70] if errors else f"{job.items_done} refs")
    event(f"flatpak: {'FAILED: ' + errors[0][:60] if errors else 'done'}")


def run_flatpak_cli(job, rundir, why):
    event(f"flatpak: no libflatpak bindings ({why[:40]}), using the CLI")
    with open(f"{rundir}/flatpak.log", "w") as log:
        p = popen(["flatpak", "update", "-y", "--noninteractive"], log)
    job.start("running")
    job.current = "flatpak update (no byte progress)"
    rc = p.wait()
    job.current = ""
    job.finish(rc)


# ---------------------------------------------------------------------------------------- tools
TG_STEP = re.compile(r"―― \d\d:\d\d:\d\d - (.+?) ――")


def topgrade_steps():
    out = subprocess.run(["topgrade", "-n", "--no-self-update"], stdin=subprocess.DEVNULL, capture_output=True,
                         text=True, env=ENV_C, timeout=120).stdout
    return [s for s in TG_STEP.findall(re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", out)) if s != "Summary"]


def tools_lanes():
    """[(lane, topgrade args)] from tools_lanes.py (~/.config/upall/tools-lanes.conf), or [] for one sequential
    `topgrade --yes`. The helper dry-runs the lanes first; why it ignored a config goes to the events."""
    helper = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools_lanes.py")
    if not os.path.exists(helper):
        return []
    try:
        r = subprocess.run([sys.executable, "-I", helper], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           env=ENV_C, timeout=300)
    except (OSError, subprocess.SubprocessError):
        return []
    if r.stderr.strip():
        event("tools: " + r.stderr.strip().splitlines()[0][:150])
    return [(f[0], f[1:]) for f in (l.split("\t") for l in r.stdout.splitlines() if l)]


def failed_sections(text, cap=20):
    """Output of each step the topgrade Summary marks FAILED (header line + up to `cap` lines); [] if none matched."""
    text = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", text)
    bad = {m.lower() for m in re.findall(r"^(.+?): FAILED", text, re.M)}
    out, show, n = [], False, 0
    for line in text.splitlines():
        m = TG_STEP.match(line)
        if m:
            show, n = m.group(1) != "Summary" and m.group(1).lower() in bad, 0
            if show:
                out.append(line)
        elif show and n < cap:
            out.append(line); n += 1
    return out


def run_tools(job, rundir, planned, lanes=None):
    """topgrade, in parallel lanes (see tools_lanes.py) or as one run. Progress = step headers seen in all the lane
    logs, against the steps of the dry run; each running lane's newest header is still in progress."""
    single = not lanes
    lanes = lanes or [("", [])]
    procs = {}
    for name, args in lanes:
        path = f"{rundir}/topgrade{'-' + name if name else ''}.log"
        with open(path, "w") as log:
            procs[name] = (popen(["topgrade", "--yes", *args], log, detach_tty=True), path)
    job.start("running")
    job.items_total = len(planned) or 1
    seen = {n: [] for n in procs}

    def read(path):
        return re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", open(path, errors="replace").read())

    def poll():
        cur, running = [], 0
        for n, (p, path) in procs.items():
            steps = [s for s in TG_STEP.findall(read(path)) if s != "Summary"]
            for s in steps[len(seen[n]):]:
                seen[n].append(s)
                event(f"tools: {s}" if single else f"tools: {n}: {s}")
            if p.poll() is None and steps:
                running += 1
                cur.append(steps[-1] if single else f"{n}: {steps[-1]}")
        total = sum(len(v) for v in seen.values())
        job.items_total = max(len(planned), total + running)
        job.items_done = max(0, total - running)
        job.current = "  ·  ".join(cur) or "starting"

    while any(p.poll() is None for p, _ in procs.values()):
        poll()
        time.sleep(0.5)
    poll()
    rc, failed, bad_log = 0, [], ""
    for n, (p, path) in procs.items():
        lane_rc = p.wait()
        lane_failed = re.findall(r"^(.+?): FAILED", read(path), re.M)
        failed += [f"{n}: {f}" if not single else f for f in lane_failed]
        if (lane_rc or lane_failed) and not bad_log:
            bad_log = path
        rc = rc or lane_rc or (1 if lane_failed else 0)
    job.items_done, job.current, job.log = job.items_total, "", bad_log
    job.finish(rc, ("failed: " + ", ".join(failed)) if failed else
               f"{sum(len(v) for v in seen.values())} steps" + ("" if single else f", {len(procs)} lanes"))
    event(f"tools: {'done' if job.rc == 0 else job.note}")


# ------------------------------------------------------------------------------------------ demo
def run_demo(job, rundir, total=0, items=0, speed=0):
    """Fake job for `upall --tui --demo`: shows the layout without touching the system."""
    job.total, job.items_total = total, items
    time.sleep(0.5)
    job.start("downloading" if total else "running")
    b = 0
    while b < total:
        b = min(total, b + speed * 0.25 * (0.6 + 0.8 * (time.time() % 1)))
        job.set_bytes(int(b))
        job.items_done = int(items * b / total)
        job.current = f"package-{job.items_done + 1}"
        time.sleep(0.25)
    job.state, job.speed = "installing", 0.0
    for i in range(items):
        job.items_done, job.current = i + 1, f"setting up package-{i + 1}"
        time.sleep(0.15)
    job.current = ""
    job.finish(0, f"{items} packages")
    event(f"{job.name}: done")


# ---------------------------------------------------------------------------------------- render
# Same look as nala's own progress (nala/rich.py): red track filled in cyan, bold blue percent and speed,
# bold green sizes, "•" separators, a dots spinner that turns into a green check, bold green border.
from rich.spinner import Spinner

TRACK, FILL = "red", "cyan"
SEP = Text(" • ", style="bold")
SPINNERS: dict[str, Spinner] = {}
PHASE_T: dict[tuple, float] = {}
PHASE = {"waiting": "Waiting", "planning": "Checking", "downloading": "Downloading", "installing": "Installing",
         "running": "Running", "done": "Done", "failed": "Failed", "skipped": "Up to date"}


def eta(j, now):
    if j.state == "downloading" and j.total and j.speed > 0:
        return (j.total - j.done) / j.speed
    if j.state in ("installing", "running") and j.items_total and j.items_done:
        t = now - PHASE_T.get((j.name, j.state), now)
        return t / j.items_done * (j.items_total - j.items_done)
    return None


def hms(sec):
    if sec is None:
        return Text("-:--:--", style="bold default")
    sec = int(sec)
    return Text(f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}")


# Width levels (terminal columns):
#   L  >= 100  two lines: header with current item + elapsed; "Time Remaining:" bar % • size • speed
#   M  72-99   same two lines, short labels ("ETA"), no elapsed column
#   S  46-71   three lines: name phase %; full-width bar; size • speed • ETA (or the current item)
#   XS < 46    no bar (too short to read): one line per source, braille spinner + name + % + speed
# Height: the Events panel gets what is left and disappears when there is no room.
def level(width):
    return "L" if width >= 100 else "M" if width >= 72 else "S" if width >= 46 else "XS"


def job_parts(j, now, bar_width=None):
    PHASE_T.setdefault((j.name, j.state), now)
    active = j.state in ("planning", "downloading", "installing", "running")
    if active:
        icon = SPINNERS.setdefault(j.name, Spinner("dots", style="bold blue"))
    elif j.state in ("done", "skipped"):
        icon = Text("✔", style="bold green")
    elif j.state == "failed":
        icon = Text("✘", style="bold red")
    else:
        icon = Text("·", style="dim")
    phase = Text(PHASE[j.state] + ("…" if active else ""),
                 style={"failed": "bold red", "done": "bold green", "skipped": "bold green",
                        "waiting": "dim"}.get(j.state, "bold blue"))
    frac = j.fraction
    bar = ProgressBar(total=1.0, completed=frac, width=bar_width,
                      pulse=j.state in ("planning", "running") and not j.items_total,
                      style=TRACK if j.state != "waiting" else "grey30",
                      complete_style="bold red" if j.state == "failed" else FILL,
                      finished_style="bold red" if j.state == "failed" else FILL)
    if j.state == "downloading" and j.total:
        size = Text(pair(j.done, j.total), style="bold green")
    elif j.items_total and j.state in ("installing", "running", "done"):
        size = Text(f"{j.items_done}/{j.items_total}", style="bold green")
    elif j.total and j.state == "done":
        size = Text(human(j.total), style="bold green")
    else:
        size = Text("")
    speed = Text(f"{human(j.speed)}/s", style="bold blue") if j.state == "downloading" and j.speed > 0 else Text("")
    return dict(active=active, icon=icon, phase=phase, frac=frac, bar=bar, size=size, speed=speed,
                pct=Text(f"{frac * 100:5.1f}%", style="bold blue"),
                eta=hms(eta(j, now)) if active else Text(""),
                what=Text(j.current or j.note or "", style="dim" if j.state in ("waiting", "skipped") else "default"),
                el=Text(clock((j.ended or now) - j.started) if j.started else "", style="dim"),
                dpkg=j.name == "apt" and j.state == "installing")


def width_ok(p):
    return True


def joined(*parts):
    """size • speed • eta, skipping empty parts."""
    out = Text()
    for t in [t for t in parts if t.plain]:
        if out.plain:
            out.append_text(SEP)
        out.append_text(t)
    return out


def grid(*cols):
    g = Table.grid(expand=True, padding=(0, 1))
    for c in cols:
        g.add_column(**c)
    return g


def job_rows(j, now, lvl, width=120):
    # S: the bar has a line of its own; size it exactly (panel borders 2 + padding 2)
    p = job_parts(j, now, bar_width=width - 5 if lvl == "S" else None)
    name = Text(j.name, style="bold")
    if lvl in ("L", "M"):
        head = grid(dict(width=1), dict(width=8, no_wrap=True), dict(width=12, no_wrap=True),
                    dict(ratio=1, no_wrap=True, overflow="ellipsis"),
                    *([dict(width=7, justify="right")] if lvl == "L" else []))
        head.add_row(p["icon"], name, p["phase"], p["what"], *([p["el"]] if lvl == "L" else []))
        if lvl == "L":
            label = ("Running dpkg" if p["dpkg"] else "Time Remaining:") if p["active"] else ""
            line = grid(dict(width=1), dict(width=15, no_wrap=True), dict(width=7, no_wrap=True), dict(ratio=1),
                        dict(width=6, justify="right"), dict(width=17, no_wrap=True), dict(width=13, no_wrap=True))
            line.add_row("", Text(label, style="bold green"), p["eta"], p["bar"], p["pct"],
                         Text.assemble(SEP, p["size"]) if p["size"].plain else Text(""),
                         Text.assemble(SEP, p["speed"]) if p["speed"].plain else Text(""))
        else:
            label = ("dpkg" if p["dpkg"] else "ETA") if p["active"] else ""
            line = grid(dict(width=1), dict(width=4, no_wrap=True), dict(width=7, no_wrap=True), dict(ratio=1),
                        dict(width=6, justify="right"), dict(width=28, no_wrap=True))
            line.add_row("", Text(label, style="bold green"), p["eta"], p["bar"], p["pct"],
                         joined(p["size"], p["speed"]))
        return [head, line]
    if lvl == "S":
        head = grid(dict(width=1), dict(width=8, no_wrap=True), dict(ratio=1, no_wrap=True, overflow="ellipsis"),
                    dict(width=6, justify="right"))
        head.add_row(p["icon"], name, p["phase"], p["pct"])
        info = joined(p["size"], p["speed"], p["eta"] if p["active"] and p["eta"].plain != "-:--:--" else Text(""))
        third = info if info.plain else p["what"]
        barline = Table.grid()              # a bare ProgressBar emits no line break; a grid row does
        barline.add_column()
        barline.add_row(p["bar"])
        rows = [head, barline]
        if third.plain:
            third.no_wrap, third.overflow = True, "ellipsis"
            rows.append(Text.assemble("  ", third))
        return rows
    # XS: the spinner (braille dots) is the progress indicator; ✔/✘ when finished
    head = grid(dict(width=1), dict(ratio=1, no_wrap=True, overflow="ellipsis"), dict(width=6, justify="right"),
                *([dict(width=11, justify="right", no_wrap=True)] if width_ok(p) else []))
    head.add_row(p["icon"], name, p["pct"], *([p["speed"]] if width_ok(p) else []))
    return [head]


def render(jobs, t0, rundir, footer="", size=None):
    width, height = size or (120, 40)
    lvl = level(width)
    now = time.monotonic()
    rows = []
    for j in jobs:
        rows += job_rows(j, now, lvl, width)
    total_speed = sum(j.speed for j in jobs if j.state == "downloading")
    busy = any(j.state in ("planning", "downloading", "installing", "running", "waiting") for j in jobs)
    title = Text("Updating…" if busy else "Finished", style="bold default")
    speed_t = Text.assemble(("↓ " + human(total_speed) + "/s", "bold blue")) if total_speed else Text("")
    if lvl in ("L", "M"):
        sub = joined(speed_t, Text(f"elapsed {clock(now - t0)}"))
    else:
        sub = speed_t if speed_t.plain else Text(clock(now - t0))
    main = Panel(Group(*rows), title=title, subtitle=sub, subtitle_align="right",
                 border_style="bold green", padding=(0, 1 if lvl != "XS" else 0))
    if lvl in ("L", "M"):
        foot = Text(footer or f"{rundir}  ·  Ctrl-C: close view (apt, snap keep going)", style="dim")
    else:
        foot = Text(footer and "done · logs in ~/.local/state/upall" or "Ctrl-C: close view", style="dim")
    foot.no_wrap, foot.overflow = True, "ellipsis"
    # height: main panel = rows + 2 borders; footer 1; events panel = lines + 2 borders
    room = height - (len(rows) + 2) - 1 - 2
    parts = [main]
    if room >= 1:
        with LOCK:
            ev = EVENTS[-min(room, 8):]
        txt = Text("\n".join(ev) if ev else "starting…", style="dim", no_wrap=True, overflow="ellipsis")
        if lvl in ("S", "XS"):   # drop the hh:mm:ss prefix
            txt = Text("\n".join(e.split("  ", 1)[-1] for e in ev) or "starting…", style="dim",
                       no_wrap=True, overflow="ellipsis")
        parts.append(Panel(txt, title="[bold default]Events", title_align="left", border_style="green"))
    parts.append(foot)
    return Group(*parts)


# ------------------------------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default=None)
    ap.add_argument("--no-tools", action="store_true")
    ap.add_argument("--after-apt", default="")
    ap.add_argument("--skip-apt", action="store_true")
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--demo", action="store_true")
    a = ap.parse_args()
    rundir = a.run or os.path.expanduser(f"~/.local/state/upall/{time.strftime('%Y%m%d-%H%M%S')}")
    os.makedirs(rundir, exist_ok=True)

    if a.plan:
        files, inst = apt_plan()
        print(f"apt:     {len(inst)} packages, {human(sum(files.values()))} to download ({len(files)} files)")
        print(f"snap:    {snap_plan() or 'up to date'}")
        try:
            _, ins = flatpak_installs()
            print(f"flatpak: {[r.get_name() for _, rs in ins for r in rs] or 'up to date'}")
        except Exception as e:
            print(f"flatpak: bindings unavailable: {e}")
        print(f"tools:   {topgrade_steps()}")
        print(f"lanes:   {[n for n, _ in tools_lanes()] or 'none (one topgrade, step after step)'}")
        return 0

    jobs = {n: Job(n) for n in ("apt", "flatpak", "snap", "tools")}
    if a.no_tools:
        del jobs["tools"]
    after = set(filter(None, a.after_apt.split(",")))
    planned_tools, lanes = [], []

    if a.demo:
        runners = {"apt": lambda j, d: run_demo(j, d, 180e6, 42, 14e6),
                   "flatpak": lambda j, d: run_demo(j, d, 60e6, 3, 9e6),
                   "snap": lambda j, d: run_demo(j, d, 200e6, 2, 11e6)}
    else:
        runners = {"apt": run_apt, "flatpak": run_flatpak, "snap": run_snap}
        if a.skip_apt:
            def skip_apt(job, rundir):
                job.state, job.note = "skipped", "skipped (not confirmed)"
                event("apt: skipped")
            runners["apt"] = skip_apt

    def worker(name):
        try:
            runners[name](jobs[name], rundir)
        except Exception as e:  # never leave a row hanging
            jobs[name].finish(1, f"internal error: {e}"[:70])
            event(f"{name}: internal error {e}")

    def tools_plan():
        nonlocal planned_tools, lanes
        try:
            planned_tools = topgrade_steps() if not a.demo else ["rustup", "pipx", "npm", "uv tools"]
            lanes = tools_lanes() if not a.demo else []
        except Exception:
            planned_tools = []
        if "tools" in jobs:
            jobs["tools"].items_total = len(planned_tools)
            jobs["tools"].current = (f"after the package group ({len(planned_tools)} steps"
                                     + (f", {len(lanes)} parallel lanes)" if lanes else ")"))

    def orchestrate():
        plan_th = threading.Thread(target=tools_plan, daemon=True)
        plan_th.start()
        first = [n for n in ("apt", "flatpak", "snap") if n not in after]
        ths = [threading.Thread(target=worker, args=(n,), daemon=True) for n in first]
        for n in after:
            jobs[n].current = "after apt (its own package is being upgraded)"
        for t in ths:
            t.start()
        ths[0].join()                       # apt
        late = [threading.Thread(target=worker, args=(n,), daemon=True) for n in after]
        for t in late:
            t.start()
        for t in ths[1:] + late:
            t.join()
        if "tools" in jobs:
            if a.demo:
                run_demo(jobs["tools"], rundir, 0, 4, 0)
            else:
                try:
                    plan_th.join(300)
                    run_tools(jobs["tools"], rundir, planned_tools, lanes)
                except Exception as e:
                    jobs["tools"].finish(1, f"internal error: {e}"[:70])

    console = Console()
    t0 = time.monotonic()
    orch = threading.Thread(target=orchestrate, daemon=True)
    orch.start()
    try:
        with Live(render(jobs.values(), t0, rundir, size=console.size), console=console, refresh_per_second=4,
                  transient=False) as live:
            while orch.is_alive():
                live.update(render(jobs.values(), t0, rundir, size=console.size))
                time.sleep(0.25)
            live.update(render(jobs.values(), t0, rundir, size=console.size, footer=f"logs: {rundir}"))
    except KeyboardInterrupt:
        console.print(f"\n[yellow]View closed. apt/dpkg and snapd keep running in the background "
                      f"(flatpak stops where it was; that is safe). Logs: {escape(rundir)}[/]")
        return 130
    bad = [j for j in jobs.values() if j.state == "failed"]
    for j in bad:
        log = j.log or f"{rundir}/{'topgrade' if j.name == 'tools' else j.name}.log"
        console.print(f"[red]✘ {escape(j.name)}[/] {escape(j.note)}  [dim]{escape(log)}[/]")
        if os.path.exists(log):
            text = open(log, errors="replace").read()
            tail = (failed_sections(text) if j.name == "tools" else []) or text.strip().splitlines()[-8:]
            console.print(Text("\n".join("    " + l for l in tail), style="dim"))
    return 1 if bad else 0


if __name__ == "__main__":
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    sys.exit(main())
