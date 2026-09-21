#!/bin/sh
# cc-dashboard installer.
#
#   From a clone:     ./install.sh
#   Without a clone:  curl -fsSL https://raw.githubusercontent.com/Cephla-Lab/cc-dashboard/main/install.sh | sh
#
#   ./install.sh --no-hook     install the dashboard only, leave ~/.claude/settings.json alone
#   ./install.sh --uninstall   remove the files, the launcher and the banner hook
#
# Installs two Python files into ~/.claude/hooks, a `cc-dashboard` launcher into ~/.local/bin,
# and (unless --no-hook) one Notification hook into ~/.claude/settings.json. The settings file is
# backed up first, is never touched when it isn't valid JSON, and an existing hook is left as is.
set -eu

REPO_RAW="https://raw.githubusercontent.com/Cephla-Lab/cc-dashboard/main"
HOOKS_DIR="$HOME/.claude/hooks"
BIN_DIR="$HOME/.local/bin"
FILES="cc_dashboard.py notify_needs_input.py"

wire_hook=yes
uninstall=no
for arg in "$@"; do
    case "$arg" in
        --no-hook) wire_hook=no ;;
        --uninstall) uninstall=yes ;;
        -h|--help)
            echo "usage: install.sh [--no-hook | --uninstall]"
            echo "  (no option)   install the dashboard and wire the banner hook into ~/.claude/settings.json"
            echo "  --no-hook     install the dashboard only, leave ~/.claude/settings.json alone"
            echo "  --uninstall   remove the files, the launcher and the banner hook"
            exit 0 ;;
        *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
    esac
done

command -v python3 >/dev/null 2>&1 || { echo "cc-dashboard needs python3 (3.9 or newer) on your PATH." >&2; exit 1; }

if [ "$uninstall" = yes ]; then
    [ -f "$HOOKS_DIR/cc_dashboard.py" ] && python3 "$HOOKS_DIR/cc_dashboard.py" --remove-hook || true
    for f in $FILES; do
        rm -f "$HOOKS_DIR/$f" "$HOOKS_DIR/__pycache__/${f%.py}".*.pyc  # only our own bytecode
    done
    rmdir "$HOOKS_DIR/__pycache__" 2>/dev/null || true  # other hooks may still use it
    rm -f "$BIN_DIR/cc-dashboard"
    echo "cc-dashboard uninstalled."
    exit 0
fi

mkdir -p "$HOOKS_DIR" "$BIN_DIR"
here=$(cd "$(dirname "$0")" 2>/dev/null && pwd || echo "")
for f in $FILES; do
    if [ -n "$here" ] && [ -f "$here/$f" ]; then
        cp "$here/$f" "$HOOKS_DIR/$f"
    else
        curl -fsSL "$REPO_RAW/$f" -o "$HOOKS_DIR/$f"
    fi
done

cat > "$BIN_DIR/cc-dashboard" <<'EOF'
#!/bin/sh
# Live list of Claude Code sessions; press a row's key to jump to its Terminal tab.
exec python3 "$HOME/.claude/hooks/cc_dashboard.py" "$@"
EOF
chmod +x "$BIN_DIR/cc-dashboard"
echo "Installed cc-dashboard into $HOOKS_DIR, launcher at $BIN_DIR/cc-dashboard."

if [ "$wire_hook" = yes ]; then
    python3 "$HOOKS_DIR/cc_dashboard.py" --install-hook || true
fi

case ":$PATH:" in
    *":$BIN_DIR:"*) echo "Run: cc-dashboard" ;;
    *) echo "Note: $BIN_DIR is not on your PATH. Add it, or run: $BIN_DIR/cc-dashboard" ;;
esac
[ "$(uname -s)" = Darwin ] || echo "Note: banners and jump-to-tab are macOS-only; the list itself works anywhere."
