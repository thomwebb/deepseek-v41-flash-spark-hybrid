#!/usr/bin/env bash
# Install the head supervisor for the current user and make it survive reboot.
#
# Runs as whoever invokes it. Nothing here assumes a username or a home path:
# systemd resolves %h (home) and %u (user) when it loads the unit.
#
# Without linger the user manager only starts at login, so an enabled unit never
# runs on a headless reboot. That is the one piece of boot wiring no unit file
# can express about itself.
#
#   ./install-supervisor.sh            install, enable linger, enable + start
#   ./install-supervisor.sh --check    report state, change nothing
#   ./install-supervisor.sh --remove   stop + disable the unit and linger
set -eu

ROOT=$(cd "$(dirname "$0")" && pwd)
UNIT=dsv41-hybrid.service
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
INSTALLED="$UNIT_DIR/$UNIT"
STAGED="$ROOT/dsv41-hybrid.service"   # repo file keeps its real name
ME=$(id -un)

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

linger_state() { loginctl show-user "$ME" --property=Linger --value 2>/dev/null || echo unknown; }
unit_state()   { systemctl --user is-enabled "$UNIT" 2>/dev/null || echo disabled; }
active_state() { systemctl --user is-active "$UNIT" 2>/dev/null || echo inactive; }

check() {
    say "user:     $ME"
    say "linger:   $(linger_state)  (must be 'yes' or the unit never starts at boot)"
    say "unit:     $(unit_state) on $INSTALLED"
    say "active:   $(active_state)"
    say ""
    say "Verify with:"
    say "  loginctl show-user \"\$(id -un)\" --property=Linger"
    say "  systemctl --user status $UNIT"
    say "  curl -fsS http://127.0.0.1:8888/health"
}

remove() {
    systemctl --user disable --now "$UNIT" 2>/dev/null || true
    rm -f "$INSTALLED"
    systemctl --user daemon-reload
    loginctl disable-linger "$ME" 2>/dev/null || true
    say "Removed $UNIT and disabled linger for $ME."
    say "Any other unit in $UNIT_DIR now also stops starting at boot."
}

# Not named 'install': that would shadow /usr/bin/install and recurse forever.
do_install() {
    [ -f "$STAGED" ] || die "missing $STAGED"
    mkdir -p "$UNIT_DIR"
    if [ -f "$INSTALLED" ] && ! cmp -s "$INSTALLED" "$STAGED"; then
        backup="$INSTALLED.previous-$(date +%Y%m%d-%H%M%S)"
        cp -p "$INSTALLED" "$backup"
        say "Existing unit differed; saved it to $backup"
    fi
    install -m 0644 "$STAGED" "$INSTALLED"
    systemd-analyze --user verify "$INSTALLED" >/dev/null
    # Linger first: enabling the unit is pointless at boot without it.
    loginctl enable-linger "$ME"
    systemctl --user daemon-reload
    systemctl --user enable "$UNIT"
    say "Enabled $UNIT and linger for $ME."
    say "Not starting it: promotion owns the switch from the original unit,"
    say "and both units declare Conflicts=. Use 'promote_grammar.py' or"
    say "'systemctl --user enable --now $UNIT' when you are ready."
    say ""
    check
}

case "${1:-}" in
    "")        do_install ;;
    --check)   check ;;
    --remove)  remove ;;
    -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//' ;;
    *)         die "unknown argument '$1' (use --check, --remove or --help)" ;;
esac
