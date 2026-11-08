# Design notes

How upall works inside, the choices behind it, and the traps found while building it. For using it, see the
[README](../README.md).

## Layout

```
bin/upall                 bash: modes, checks, safety, parallel jobs, spinner, summary
lib/upall/tui.py          Python + rich: the --tui live view; runs the same jobs with finer progress
lib/upall/pk_refresh.py   Python + gi: one PackageKit RefreshCache over D-Bus
lib/upall/secwatch.py     Python + gi: user service, login/resume checks, dpkg.log watcher, ntfy pushes
systemd/                  the user unit for secwatch
sbin/upall-root           bash, root-owned when installed: the only commands upall runs as root
sudoers/                  passwordless rule template (@USER@): allows only sbin/upall-root
examples/                 topgrade.toml, ntfy.conf, secwatch.conf
```

## Why bash for upall

`upall` starts other CLIs, puts them in the background and waits: that is what bash is good at, and it needs no
dependencies. The TUI is in Python because it parses progress from several APIs and draws with rich (which ships
with nala, so it is already installed). `upall` can be sourced (`source bin/upall`) to load its functions for
tests without running anything.

## Update flow

1. **sudo probe**: `sudo -n /usr/local/sbin/upall-root ping`. `sudo -l` can't be used: it lists commands that
   need a password too. Without the rule: `sudo -v` once and a keepalive loop every 50 s, using the root-owned
   helper if installed, else the copy in `~/.local/lib/upall`. The helper path is exported as `UPALL_ROOT` for the TUI.
2. **apt lists**: `upall-root update` behind a spinner.
3. **serial check**: if the upgrade contains `snapd` or `flatpak`/`libflatpak0`, that manager runs after apt.
4. **apt_safety**: `apt-get -s -o Debug::NoLocking=1 full-upgrade`; parses `Inst`/`Remv` lines; counts
   new `linux-image-[0-9]*` packages against installed kernels and free space in `/boot`. The auto-removals
   come from python-apt, with the same calls nala makes (`Cache.upgrade(dist_upgrade=True)`, then every
   installed package that `is_auto_removable`); they count as apt work, so a run that only auto-removes still
   gets a snapshot.
5. **pre_apt_snapshot**: `upall-root ts-create` (Timeshift `--create --comments upall-pre-apt --scripted`), then
   `upall-root ts-prune 2`: from `timeshift --list --scripted`, rows whose comment is *exactly* `upall-pre-apt`
   (the tag letters are skipped), oldest first, all but the newest 2.
6. **parallel group**: nala ‖ flatpak ‖ snap, each to `$RUN/<name>.log`, exit code and duration to
   `$RUN/<name>.rc`. `wait_group` polls the `.rc` files so each job is reported the moment it ends.
7. **topgrade** `--yes` (skipped by `--system`), after `upall-root home-snapshot` (read-only snapshot of `/home`,
   which Timeshift doesn't cover) and `upall-root home-prune 2 14`.
8. **firmware** check (`fwupdmgr get-updates --json`, using the metadata `fwupd-refresh.timer` already fetched),
   **PackageKit refresh**, **summary** with per-source exit codes and "Reboot required" if
   `/var/run/reboot-required` exists.

nala runs as `nala upgrade --full`. Without `--full`, nala follows `full_upgrade = false` in
`/etc/nala/nala.conf` (its default), which is `apt-get upgrade`: no new packages and no removals, so a new kernel
(a new `linux-image-*` package pulled in by `linux-generic`) is held back forever, and the safety check would
be judging a plan that never runs. The flag keeps the setting local to upall; interactive `nala` is unchanged.

apt options everywhere: `DPkg::Lock::Timeout=300` (Discover/packagekitd often hold the lock for a while),
`--force-confdef --force-confold`, `DEBIAN_FRONTEND=noninteractive`. nala runs with `--no-update` (lists were
just refreshed) and `-o Nala::scrolling_text=false` (clean logs).

## Ctrl-C and process groups

Interrupting dpkg can leave the system half-configured, so package jobs must survive Ctrl-C:

- **bash**: background jobs of a non-interactive script ignore SIGINT already. Verified under a pty with sudo's
  `use_pty` (the Ubuntu default): a `sudo -n …` job started with `&` survives a terminal SIGINT.
- **Python**: a child does *not* survive unless started in its own process group (`process_group=0`), so the TUI
  starts apt and snap that way. Not `start_new_session`: sudo's cached ticket is per terminal.
- **topgrade** is the exception: it is started with `start_new_session=True` (no controlling terminal). Steps like
  SDKMAN run `bash -i`, which tries to take the terminal; from a background process group the kernel stops it
  with SIGTTOU/SIGTTIN forever (state `T`). Without a controlling terminal `bash -i` simply runs without job
  control.
- `kill -0` on a root PID fails for the user, so `spin` runs its command in a subshell and polls the subshell.

## The TUI's progress sources

Each source reports through the most precise channel it has:

| Source | Progress |
|---|---|
| apt | `apt-get --print-uris -qq full-upgrade` gives the .deb files and sizes nala will fetch. Progress = planned files already in `/var/cache/apt/archives` + sizes in `archives/partial` (root/_apt only: `upall-root apt-partial`, once a second). Then `Setting up` lines in the log for the install phase. |
| snap | `upall-root snap-refresh --no-wait` prints a change id; per-task byte progress from the snapd REST API on `/run/snapd.socket` (`/v2/changes/{id}`), readable as the user. |
| flatpak | runs in-process with libflatpak (`gi.repository.Flatpak` Transaction: `new-operation`, progress `changed`, `operation-done`/`operation-error`). Falls back to the `flatpak` CLI without byte progress if the bindings are missing. |
| tools | topgrade's log: step headers (`―― HH:MM:SS - Step ――`) counted against a `topgrade -n` dry run. |

The look follows nala's own progress bar: red track filled in cyan, `•` separators, a dots spinner that turns
into a green ✔. Layout by width, re-measured every frame:

| Width | Layout |
|---|---|
| ≥ 100 | two lines per source, full labels, elapsed per source |
| 72–99 | two lines, short labels, no elapsed column |
| 46–71 | three lines: name/phase/%, a bar of its own (`width - 5`), then size • speed • ETA |
| < 46 | one line: braille spinner, name, %, speed; no bar |

The events panel gets whatever height is left and disappears when there is none.

Rich gotchas: a bare `ProgressBar` in a `Group` emits no line break, so it must sit in a `Table.grid` row; rich's
SVG export shows blank lines a real terminal doesn't.

## Units and locale

All sizes and speeds are IEC (B, KiB, MiB, GiB, TiB; 1 KiB = 1024 B). Done/total pairs use the total's unit
("0.8/1.1 GiB"). awk runs under `LC_ALL=C` and bash uses `${EPOCHREALTIME/[.,]/}`, because a Catalan or Spanish
locale writes decimal commas. The plain-mode speed is the whole machine's receive rate from `/proc/net/dev`
(all interfaces but `lo`): apt, Flatpak and Snap don't report bytes in plain mode.

## Discover sync

Discover keeps its own list through PackageKit, and stays stale after updates done elsewhere. `pk_refresh.py`
calls `CreateTransaction` and `RefreshCache(false)` on the system bus; polkit allows `system-sources-refresh` for
the active user, so no password. Both calls must come from the **same** D-Bus connection (a transaction belongs to
the connection that created it; `busctl` per call fails with "sender does not match"). PackageKit then emits
`UpdatesChanged` and Discover and its tray notifier rebuild their lists.

## secwatch

- **Triggers**: login (`LOGIN_DELAY` after the service starts in `graphical-session.target`) and resume (logind
  `PrepareForSleep(false)`). A timer makes no sense on a PC that sleeps when idle.
- **Security updates** = `apt list --upgradable` rows whose suite contains `-security`.
- **Auto apply** runs `upall --system` with stdin from `/dev/null` and `TERM=dumb`: no spinner, and every
  question upall would ask is answered "no", so only safe plans run.
- **Applied updates (dpkg.log watcher)**. The goal was a push when security updates are applied by *any*
  installer, Discover included. An apt hook (`DPkg::Post-Invoke`) would run as root inside apt, would need to
  read the user's ntfy settings, and would miss nothing that dpkg.log doesn't already record. Instead secwatch
  polls the size of `/var/log/dpkg.log` every 30 s, waits for 30 s of quiet (one push per run), reads the new
  `upgrade`/`install` lines from a saved offset (handling rotation by inode), and asks `apt-cache madison`
  whether each new version is offered by a `*-security` pocket. KDE neon's own packages never count.
  `SECWATCH_DPKG_LOG` overrides the path for tests.
- **ntfy**: plain HTTP POST with `Title`, `Priority`, `Tags` and `Click` headers; never raises (network down =
  only the desktop notification).
- **No reboot, anywhere.** Nothing calls `reboot`, `shutdown`, `systemctl reboot/poweroff/suspend`, or `kexec`;
  `unattended-upgrades` (which has an automatic-reboot option) isn't used.

## Root helper and sudoers

The first version allowed `nala` and `apt-get` with any arguments, plus pinned `snap`, `timeshift`, `btrfs` and
`ls` calls, and kept `DEBIAN_FRONTEND` through `env_keep`. A security audit found three problems with that: `nala`/
`apt-get -o …` can run any command as root (passwordless root for any process running as the user); the Timeshift
delete rule accepted any snapshot name (sudo can't check a comment); and an unchecked `DEBIAN_FRONTEND` lets a
caller pick debconf's `Editor` frontend, a root editor if a package asks a question.

`sbin/upall-root` replaces all of it. Rules it follows:
- Fixed verbs; arguments are validated **before** the root check (exit 2), so CI tests them without root.
  Exit codes: 0 ok, 1 failed, 2 usage, 3 precondition, 4 not root.
- `PATH`, `LC_ALL`, `umask`, `DEBIAN_FRONTEND` are set by the script; sudo's `env_reset` drops the caller's.
- Snapshot selection is done in pure functions (`ts_select`, `home_select`) covered by `upall-root selftest`.
- `/home/.snapshots` must be a real directory owned by root and not group/other-writable (created that way on
  first use), and each subvolume is checked (`inode 256`, not a symlink) before `btrfs subvolume delete`: nobody
  can redirect a snapshot or a delete with a planted symlink.
- Long actions `exec` the real command, so signals and exit codes pass through unchanged.

Other notes: `env` must never be allowed in a NOPASSWD rule (it runs anything). Timeshift 24.01 rejects
`--tags O` although `--help` lists it; O (on demand) is the default, so it is left out.

## Tested, and not yet

Tested on the original machine (2026-11-07): `upall check`; `upall --system` and `upall --tui` end to end (incl.
topgrade, 22 steps); the safety checks with a fake `apt-get` (removals + third kernel: asked; no tty: skipped;
routine plan: no question); snapshot create/prune; PackageKit refresh; the TUI layouts with `--demo`; secwatch
login check and pushes; the dpkg.log watcher with a fake log (baseline: no push; openssl from `-security` flagged,
a Plasma update ignored; rescan: nothing).

Not yet seen in a real event: the resume trigger, an unattended security install, the dpkg watcher on a real
Discover security update, the TUI's byte bars on large real downloads.
