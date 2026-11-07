# upall

[![CI](https://img.shields.io/github/actions/workflow/status/clicraft/upall/ci.yml?label=CI&style=flat-square)](https://github.com/clicraft/upall/actions/workflows/ci.yml)
[![License](https://img.shields.io/github/license/clicraft/upall?style=flat-square)](LICENSE)

One command to check and update every package source on an Ubuntu-based desktop (built on KDE neon 24.04):
**apt** (through nala), **Flatpak** and **Snap** in parallel, then **topgrade** for user-level tools (rustup,
pipx, uv tools, npm globals, VS Code extensions, gh extensions…). Firmware updates are checked and only applied
when you ask for them.

It comes with **upall-secwatch**, a small user service that checks for Ubuntu **security** updates after you log
in and after the PC wakes up, installs them unattended (optional), and tells you on the desktop and on your phone.
**Nothing in this project ever reboots the PC.** When an update needs a reboot it says so, and you choose when.

```
upall check          what is outdated, everywhere (read-only, ~7 s)
upall                update everything
upall --tui          the same, as a live progress view
```

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Install](#install)
- [Usage](#usage)
- [Safety](#safety)
- [Restoring one tool](#restoring-one-tool)
- [The root helper and the sudoers rule](#the-root-helper-and-the-sudoers-rule)
- [Security alerts: upall-secwatch](#security-alerts-upall-secwatch)
- [Files, logs and state](#files-logs-and-state)
- [Tuning for another machine](#tuning-for-another-machine)
- [Troubleshooting](#troubleshooting)
- [Uninstall](#uninstall)

How it works inside, and why it is built this way: [docs/design.md](docs/design.md).

## Features

- **Parallel**: nala, Flatpak and Snap use separate locks, so they download and install at the same time. nala
  itself downloads in parallel from several mirrors (`sudo nala fetch` picks the fastest ones).
- **One check for everything**: `upall check` queries apt, Flatpak, Snap, fwupd, rustup and npm in parallel and
  prints one list; apt security fixes are marked `⚠` and counted.
- **Live TUI** (`--tui`): one row per source with a nala-style bar, percentage, bytes, speed and the current
  package. The layout adapts to the terminal width (full, compact, three-line, spinner-only) and follows resizes.
- **Plain mode** keeps the terminal readable: a braille spinner with elapsed time and total download speed
  (`↓ 12.4 MiB/s`, IEC units) while waiting, and each job reported the moment it finishes.
- **Safe by default**: a simulated apt plan first, questions only when it is risky, a Timeshift snapshot before
  apt, config files kept, package jobs that survive Ctrl-C. See [Safety](#safety).
- **Discover stays in sync**: at the end, PackageKit refreshes its cache, so Discover and its tray icon stop
  showing updates that are already installed.
- **Security alerts** after login and resume (not on a timer: a sleeping PC has nothing to check), with optional
  unattended install and phone pushes through [ntfy](https://ntfy.sh).
- **Logs** for every run, one file per source.

## Requirements

Ubuntu 24.04 or a derivative (KDE neon, Kubuntu…). Tested on KDE neon with Plasma 6, sudo 1.9.15, Timeshift 24.01
on btrfs.

| Needed | Package / install | Used for |
|---|---|---|
| bash 5, python3 | preinstalled | |
| nala | `sudo apt install nala` | apt upgrades with parallel downloads (falls back to `apt-get` without it) |
| python3-rich | comes with nala | the TUI |
| python3-gi | preinstalled on desktops | secwatch, PackageKit refresh, Flatpak progress |
| topgrade | `uv tool install topgrade` (or cargo/binary) | user-level tools; skip with `--system` |

Optional: `flatpak`, `snap`, `timeshift` (snapshot before apt), `fwupdmgr` (firmware), `gir1.2-flatpak-1.0`
(byte progress for Flatpak in the TUI), `wezterm` (the "Update now" button opens it), `notify-send` and
`nm-online` (secwatch), the ntfy app on your phone. Every optional piece is skipped when it is missing.

## Install

```bash
git clone https://github.com/clicraft/upall.git
cd upall
./install.sh                 # copies files to ~/.local, never touches root
./install.sh --secwatch      # the same, and enables the security-alert service
```

`install.sh` copies `upall` to `~/.local/bin`, the Python helpers to `~/.local/lib/upall`, the systemd user unit,
and the example configs (only if you don't have them yet: `~/.config/topgrade.toml`, `~/.config/upall/secwatch.conf`).
It lists missing dependencies, and prepares the root side: it runs the root helper's self-test, fills your user
name into the sudoers rule, checks it with `visudo`, and prints the two `sudo install` commands for you to run.
Run it again after `git pull`; it only copies what changed, tells you if the running service needs a restart, and
prints the helper's install command again when the helper changed.

Optional, once: `sudo nala fetch` to choose the fastest mirrors (pick 3 or so, in or near your country).

## Usage

| Command | What it does |
|---|---|
| `upall check` | List what is outdated in apt, Flatpak, Snap, firmware, rustup and npm. Read-only. |
| `upall` | Update: apt + Flatpak + Snap in parallel, then topgrade, then a firmware check. |
| `upall --system`, `-s` | Only apt + Flatpak + Snap (no topgrade). This is what secwatch runs. |
| `upall --tui`, `-t` | The update as a live table. Combine with `--system`. |
| `upall --tui --demo` | Preview the table with fake jobs; changes nothing. |
| `upall --ask`, `-a` | Always show the apt plan (upgrades, new packages, removals, auto-removals) and ask before running it. |
| `upall --firmware`, `-f` | Also apply firmware updates with `fwupdmgr update` (it asks before any reboot). |
| `upall --help` | Short help. |

Typical update run (plain mode):

```
✔ apt lists refreshed in 0m04s
apt plan: 12 to upgrade/install (0 new), 0 to remove, 0 to auto-remove; /boot 268 MB free, 2 kernel(s) installed
✔ snapshot 2026-11-07_13-37-00 (roll back: Timeshift, or sudo timeshift --restore --snapshot 2026-11-07_13-37-00)

== apt + Flatpak + Snap in parallel (logs: ~/.local/state/upall/20261107-133701) ==
✔ flatpak  done in 0m09s  nothing to do
✔ snap     done in 0m21s  All snaps up to date.
⠼ running: apt 0m48s                                            ↓ 18.3 MiB/s
```

In the TUI, Ctrl-C closes the view only: apt, dpkg and snapd keep running to the end (interrupting dpkg is how
systems break). The same goes for plain mode.

## Safety

`upall` updates without asking in the normal case. These rules decide when it stops to ask, and what protects you
when something goes wrong:

1. **The apt plan is simulated first** (`apt-get -s full-upgrade`). upall asks `[y/N]` only if the plan
   **removes** packages, or brings a **new kernel** while two are installed or `/boot` is short of space. With
   nobody at the terminal to answer (secwatch, cron), the answer is "no": apt is skipped and the rest still runs.
   The packages nala will **auto-remove** afterwards (nothing needs them any more, mostly old kernels) are listed
   on every run, in the `apt plan:` line, but are expected and not a reason to ask. `--ask` shows the whole plan.
   apt runs as a **full upgrade** (`nala upgrade --full`), the same plan that was simulated: nala's own default
   holds back anything that needs a new package, which includes every new kernel.
2. **A Timeshift snapshot is taken before apt** (comment `upall-pre-apt`); the newest 3 of upall's own are kept,
   Timeshift's scheduled snapshots are left alone. If the snapshot fails, upall asks whether to go on without one
   (no terminal: apt is skipped). No Timeshift installed: a note, and apt runs without a snapshot.
3. **Config files you changed are kept** (`--force-confold`), and dpkg never stops to ask about them.
4. **Flatpak and Snap are transactional**: a failed update leaves the old version in place, and Snap can
   `snap revert`.
5. **If snapd or flatpak themselves are in the apt upgrade**, those managers wait until apt is done.
6. **Firmware is never applied unless you pass `--firmware`.**
7. **A read-only btrfs snapshot of `/home` is taken before topgrade.** The tools topgrade updates (rustup, pipx,
   uv, npm, VS Code extensions…) live in your home folder, which Timeshift doesn't cover. Kept: the newest 3, none
   older than 14 days. To undo one tool, copy its folder back: see [Restoring one tool](#restoring-one-tool).
   Skipped when `/home` isn't its own btrfs subvolume; if the snapshot fails, topgrade runs anyway (each tool can
   also be reinstalled at an older version).
8. **No reboots.** No code path reboots, powers off, suspends or kexecs. "Reboot required" is only printed (plain
   mode, desktop notification, phone push).

## Restoring one tool

Snapshots live in `/home/.snapshots/upall-pre-tools.<date>-<time>`, read-only, with your files' own permissions,
so you can copy from them without root:

```bash
ls /home/.snapshots/                                        # pick the one taken before the bad update
S=/home/.snapshots/upall-pre-tools.20261107-201754/$USER
diff -rq "$S/.local/share/pipx/venvs/mkdocs" ~/.local/share/pipx/venvs/mkdocs   # what the update changed
rsync -a --delete "$S/.local/share/pipx/venvs/mkdocs/" ~/.local/share/pipx/venvs/mkdocs/   # put it back
```

Common folders: `~/.rustup` and `~/.cargo/bin` (Rust), `~/.local/share/pipx` (pipx), `~/.local/share/uv/tools`
(uv tools), `~/.vscode/extensions` (VS Code), `~/.sdkman/candidates` (SDKMAN), `~/.ghcup` (GHCup),
`~/.npm-global/lib/node_modules` (npm, if that is your prefix).

Don't roll back all of `/home`: that would also undo every document and setting changed since. Snapshots keep
deleted files too, so a file you delete stays in the snapshots for up to 14 days. `du` on `/home` now counts the
snapshots as well.

## The root helper and the sudoers rule

Every command upall runs as root goes through one small, root-owned script: **`upall-root`** (`sbin/upall-root`,
installed as `/usr/local/sbin/upall-root`). It accepts a fixed list of actions and nothing else: no options, no
paths, no environment from the caller.

| Action | Runs as root |
|---|---|
| `ping`, `version` | nothing (checks that the helper is reachable) |
| `update`, `update-quiet` | `apt-get update -q` / `-qq` |
| `upgrade` | `nala upgrade --full -y --no-update` with upall's apt options (`apt-get full-upgrade` + `autoremove` without nala), `DEBIAN_FRONTEND=noninteractive` |
| `snap-refresh [--no-wait\|--list]` | `snap refresh` |
| `apt-partial` | `ls -ln /var/cache/apt/archives/partial` (TUI download progress) |
| `ts-create`, `ts-prune KEEP` | Timeshift snapshot with the comment `upall-pre-apt`; delete those (only those) beyond the newest KEEP |
| `home-snapshot`, `home-prune KEEP DAYS` | read-only btrfs snapshot of `/home` in `/home/.snapshots` (root-owned, created on first use); delete upall's own beyond KEEP or older than DAYS, never the newest |
| `selftest` | checks the snapshot-selection logic (no root needed; also run in CI) |

`sudoers/upall-nopasswd` lets **your user only** run that helper without a password. It does not allow apt, nala,
snap, timeshift or btrfs directly, so a program running as you can't use the rule to run its own commands as
root: the worst it can do is update the system from your configured repositories, or create and prune upall's
own snapshots. Other Timeshift snapshots, restores and anything else still need your password.

Without the rule (or without the root-owned helper), `upall` asks for your password once per run and keeps the
ticket alive; secwatch then can't refresh the apt lists or install anything.

`install.sh` prints the commands. By hand:

```bash
./sbin/upall-root selftest                                     # must print "selftest: ok"
sed "s/@USER@/$USER/" sudoers/upall-nopasswd > /tmp/upall-nopasswd
visudo -c -f /tmp/upall-nopasswd                               # must say "parsed OK"
sudo install -m 0755 -o root -g root sbin/upall-root /usr/local/sbin/upall-root
sudo install -m 0440 -o root -g root /tmp/upall-nopasswd /etc/sudoers.d/upall-nopasswd
sudo -n /usr/local/sbin/upall-root ping                        # "ok", with no password prompt
```

The helper must stay root-owned in a root-owned folder: a copy anywhere you can write to would let anything running
as you change what it does. After `git pull`, reinstall it when `install.sh` says it changed (upall also warns when
the installed helper's version differs).

## Security alerts: upall-secwatch

`lib/upall/secwatch.py` runs as a systemd **user** service inside the graphical session
(`systemd/upall-secwatch.service`).

**When it checks:** 45 s after login and 20 s after waking from sleep or hibernation (logind's `PrepareForSleep`
signal), at most once every 3 hours. No timer: a sleeping PC has nothing to check.

**What it does:**

1. Waits for the network, refreshes the apt lists, and lists upgradable packages from a `*-security` pocket.
2. If there are any and `AUTO_APPLY=1` (default), runs `upall --system` unattended. All the [safety](#safety)
   rules apply, and since nobody can answer, a risky plan or a failed snapshot means nothing is installed.
3. Whatever is still pending gets a desktop notification (and a phone push): **"N security updates need you"**
   after a failed automatic attempt, or "N security updates pending" with `AUTO_APPLY=0`. The notification has
   two buttons: **Update now** opens `upall --tui` in a new WezTerm window; **Details** opens Ubuntu's CVE page for
   each source package (up to 5). The same set isn't announced again for 20 hours.

**"Applied" pushes, whoever installs.** The service also follows `/var/log/dpkg.log`, which every installer writes:
Discover, apt, nala, upall, and Discover's offline updates applied at boot. When dpkg has been quiet for 30 s after
installing something, it checks which new versions came from a `*-security` pocket and sends one push:
"N security updates applied", plus "Still pending: …" and "Reboot needed (not done automatically)" when they apply.
This needs no root code and no apt hook. On first start it only records where the log ends, so past updates are
not announced.

**Setup:**

```bash
./install.sh --secwatch
cp examples/ntfy.conf ~/.config/upall/ntfy.conf && chmod 600 ~/.config/upall/ntfy.conf
# put your own long random topic in it, subscribe to it in the ntfy phone app, then:
python3 ~/.local/lib/upall/secwatch.py --test-push
```

The ntfy topic is the only secret: anyone who knows it can read and post. Keep `ntfy.conf` out of git
(`.gitignore` already does). Without it, secwatch only shows desktop notifications.

Turn automatic installing off with `AUTO_APPLY=0` in `~/.config/upall/secwatch.conf` (see `examples/secwatch.conf`).

**Commands:**

```bash
journalctl --user -u upall-secwatch -f         # what it did and why
python3 ~/.local/lib/upall/secwatch.py --force      # check now, ignoring the 3 h / 20 h limits
python3 ~/.local/lib/upall/secwatch.py --once       # check now, respecting them
python3 ~/.local/lib/upall/secwatch.py --scan-dpkg  # report security updates dpkg installed since the last scan
python3 ~/.local/lib/upall/secwatch.py --push "Title" "Message" [tags]
```

## Files, logs and state

| Path | What |
|---|---|
| `~/.local/bin/upall` | the command (bash) |
| `~/.local/lib/upall/tui.py` | `--tui` live view (Python, rich) |
| `~/.local/lib/upall/pk_refresh.py` | PackageKit cache refresh for Discover |
| `~/.local/lib/upall/secwatch.py` | security-alert service |
| `~/.config/systemd/user/upall-secwatch.service` | its unit |
| `~/.config/topgrade.toml` | which topgrade steps run ([example](examples/topgrade.toml)) |
| `~/.config/upall/ntfy.conf` | phone push topic (secret, mode 600) |
| `~/.config/upall/secwatch.conf` | `AUTO_APPLY` |
| `~/.local/state/upall/<date-time>/` | one run: `apt.log`, `flatpak.log`, `snap.log`, `apt-update.log`, `snapshot.log`, `packagekit.log`, and `topgrade.log` in TUI mode |
| `~/.local/state/upall/secwatch-upall-*.log` | unattended runs started by secwatch |
| `~/.local/state/upall/secwatch.json` | secwatch state: last check, last notified set, dpkg.log offset |
| `~/.local/lib/upall/upall-root` | copy of the root helper, used with your password when the root-owned one isn't installed |
| `/usr/local/sbin/upall-root` | the root helper (root-owned) |
| `/etc/sudoers.d/upall-nopasswd` | the passwordless rule (allows only the helper) |
| `/home/.snapshots/upall-pre-tools.*` | read-only `/home` snapshots taken before topgrade |

## Tuning for another machine

upall was written for one KDE neon desktop and keeps a few of its choices as constants near the top of each file:

- `bin/upall`: `KERNEL_MB=120` (size of one kernel + initramfs in `/boot`), `SNAP_KEEP=3` (Timeshift),
  `TOOLS_KEEP=3` and `TOOLS_MAX_DAYS=14` (`/home` snapshots).
- `lib/upall/secwatch.py`: `LOGIN_DELAY` (45 s; the network comes up ~30 s after login here), `RESUME_DELAY`,
  `MIN_INTERVAL`, `RENOTIFY`. Push titles start with the short host name.
- The "Update now" button uses `wezterm`; replace it with `konsole -e` or your terminal.
- `examples/topgrade.toml` disables steps for reasons specific to that machine (comments explain each one).

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `apt-get update failed` right after boot, "could not resolve" | DNS isn't up yet; run again in a minute. |
| apt waits a long time at the start | Discover or PackageKit holds the apt lock; upall waits up to 5 min (`DPkg::Lock::Timeout=300`). |
| "no snapshot: upall-root isn't passwordless yet" | Install the root helper and the sudoers rule (`./install.sh` prints the commands). |
| "/usr/local/sbin/upall-root is version N, upall ships M" | The helper changed: rerun `./install.sh` and run the helper install command it prints. |
| "apt skipped" with nobody asked | The plan was risky and there was no terminal to confirm. Run `upall --ask` yourself. |
| Discover still lists updates you installed | Discover was open before; press "Refresh" once. upall refreshes PackageKit at the end of each run. |
| A topgrade step fails | Plain mode prints topgrade's output; in the TUI see `topgrade.log` in the run's log folder. Disable the step in `~/.config/topgrade.toml` if it keeps failing. |
| No phone pushes | `secwatch.py --test-push`; check `~/.config/upall/ntfy.conf` and the topic in the app. |

## Uninstall

```bash
systemctl --user disable --now upall-secwatch
rm -r ~/.local/bin/upall ~/.local/lib/upall ~/.config/systemd/user/upall-secwatch.service ~/.config/upall
rm -rf ~/.local/state/upall
sudo rm /etc/sudoers.d/upall-nopasswd /usr/local/sbin/upall-root
```

Timeshift snapshots named `upall-pre-apt` stay until you delete them in Timeshift. `/home` snapshots: list them
with `ls /home/.snapshots`, delete each with `sudo btrfs subvolume delete /home/.snapshots/upall-pre-tools.<…>`.

## License

MIT, see [LICENSE](LICENSE).
