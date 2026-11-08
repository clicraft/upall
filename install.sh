#!/usr/bin/env bash
# Install (or update) upall for the current user. Safe to run again: it only copies files that changed and
# never overwrites your own configs.
#
#   ./install.sh               upall, its Python helpers, the secwatch unit (not enabled), example configs
#   ./install.sh --secwatch    also enable and (re)start the upall-secwatch user service
#
# Root is never used here. The root helper (sbin/upall-root) and the sudoers rule that allows it are checked
# here; the commands that install them as root are printed for you to run.
set -euo pipefail
cd "$(dirname "$0")"

BIN=$HOME/.local/bin
LIB=$HOME/.local/lib/upall
UNIT_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
CONF_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/upall
secwatch=0
for a in "$@"; do
    case $a in
        --secwatch) secwatch=1 ;;
        -h|--help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $a" >&2; exit 2 ;;
    esac
done

changed=()
put() {  # put MODE SRC DEST: copy only if different
    if ! cmp -s "$2" "$3" 2>/dev/null; then
        install -D -m "$1" "$2" "$3"; changed+=("$3"); echo "installed $3"
    fi
}
put_once() {  # put_once MODE SRC DEST: copy only if DEST doesn't exist (user configs)
    if [[ -e $3 ]]; then echo "kept      $3 (yours; example in $2)"
    else install -D -m "$1" "$2" "$3"; echo "installed $3 (from $2)"; fi
}

put 0755 bin/upall                 "$BIN/upall"
put 0644 lib/upall/tui.py          "$LIB/tui.py"
put 0644 lib/upall/pk_refresh.py   "$LIB/pk_refresh.py"
put 0644 lib/upall/secwatch.py     "$LIB/secwatch.py"
put 0755 sbin/upall-root           "$LIB/upall-root"     # used with your password until the root-owned copy exists
put 0644 systemd/upall-secwatch.service "$UNIT_DIR/upall-secwatch.service"
put_once 0644 examples/topgrade.toml "${XDG_CONFIG_HOME:-$HOME/.config}/topgrade.toml"
put_once 0644 examples/secwatch.conf "$CONF_DIR/secwatch.conf"
((${#changed[@]})) || echo "upall files already up to date"

# ntfy.conf holds a secret topic: never created from the placeholder, only explained.
[[ -e $CONF_DIR/ntfy.conf ]] || echo "phone pushes: optional, see examples/ntfy.conf (copy to $CONF_DIR/ntfy.conf, chmod 600)"

case ":$PATH:" in *":$BIN:"*) ;; *) echo "note: $BIN is not in your PATH" ;; esac

# Dependencies: report what is missing, install nothing.
missing=()
for c in nala python3 topgrade notify-send nm-online; do command -v "$c" >/dev/null || missing+=("$c"); done
python3 -c 'import rich' 2>/dev/null || missing+=("python3-rich")
python3 -c 'import gi; gi.require_version("Flatpak", "1.0"); from gi.repository import Flatpak' 2>/dev/null \
    || echo "optional: gir1.2-flatpak-1.0 (the TUI falls back to the flatpak CLI without it)"
for c in flatpak snap timeshift fwupdmgr wezterm; do command -v "$c" >/dev/null || echo "optional: $c not found"; done
((${#missing[@]})) && echo "MISSING: ${missing[*]}  (see README.md, Requirements)"

# systemd user unit
if command -v systemctl >/dev/null; then
    systemctl --user daemon-reload
    if ((secwatch)); then
        systemctl --user enable upall-secwatch.service >/dev/null
        systemctl --user restart upall-secwatch.service
        echo "upall-secwatch enabled and (re)started: journalctl --user -u upall-secwatch -f"
    elif systemctl --user is-active -q upall-secwatch.service \
         && printf '%s\n' "${changed[@]}" | grep -q 'secwatch'; then
        echo "upall-secwatch is running an older version: systemctl --user restart upall-secwatch"
    fi
fi

# Root helper + sudoers rule: prepare and check; you install them.
helper=$PWD/sbin/upall-root
"$helper" selftest >/dev/null || { echo "WARNING: sbin/upall-root selftest failed; not suggesting it"; exit 1; }
# The filled-in rule is kept next to the helper copy (your home, not /tmp), and checked again by the command
# you run, right before it is installed.
rule=$LIB/upall-nopasswd
mkdir -p "$LIB"
sed "s/@USER@/$(id -un)/g" sudoers/upall-nopasswd >"$rule"
chmod 0644 "$rule"
if ! /usr/sbin/visudo -c -q -f "$rule" >/dev/null 2>&1; then
    echo "WARNING: visudo rejects the generated sudoers file ($rule); not suggesting it"; exit 1
fi
root_cmds=()
cmp -s "$helper" /usr/local/sbin/upall-root ||
    root_cmds+=("sudo install -m 0755 -o root -g root $helper /usr/local/sbin/upall-root")
# /etc/sudoers.d is root-only, so test what matters: does the helper already run without a password?
sudo -n /usr/local/sbin/upall-root ping >/dev/null 2>&1 ||
    root_cmds+=("sudo visudo -c -f $rule && sudo install -m 0440 -o root -g root $rule /etc/sudoers.d/upall-nopasswd")
if ((${#root_cmds[@]})); then
    echo
    echo "Root helper and passwordless rule for unattended runs (read them first: $helper, $rule):"
    printf '  %s\n' "${root_cmds[@]}"
else
    echo "upall-root: installed, up to date and passwordless"
fi
