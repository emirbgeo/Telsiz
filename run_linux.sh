#!/usr/bin/env bash
# Starts Telsiz on Linux:   bash run_linux.sh
#
# The first run checks the system packages Telsiz needs, offers to install the
# missing ones, sets up a private Python environment in .venv, and adds Telsiz
# to your applications menu. Later runs just start it.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$HERE/.venv"
PY="${TELSIZ_PYTHON:-python3}"

# pynput is installed without its dependency "evdev": that one has no ready-made
# Linux package and would need a C compiler, and Telsiz only uses pynput's X11
# backend, which never loads it. The tray library is skipped: on Linux, closing
# the window quits Telsiz. The window itself is pywebview on WebKitGTK, whose
# Python bindings (PyGObject) come from the system, so the venv sees system packages.
DEPS=(numpy ">=1.24" sounddevice ">=0.4.6" av ">=14" Pillow ">=10" six "" python-xlib ">=0.17" mss ">=9" pywebview ">=6.2")
PYNPUT="pynput>=1.7"
STAMP_TEXT="v2 ${DEPS[*]} $PYNPUT"

say() { printf '%s\n' "$*"; }

if ! command -v "$PY" >/dev/null 2>&1; then
    say "Python 3 was not found. Install it with your package manager (package name: python3), then run this again."
    exit 1
fi
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
    say "Telsiz needs Python 3.9 or newer; this system has $("$PY" -V 2>&1)."
    exit 1
fi

# ---------- system packages ----------
missing=()
# the window: GTK and WebKit with their Python bindings
"$PY" -c 'import gi
gi.require_version("Gtk", "3.0")
try:
    gi.require_version("WebKit2", "4.1")
except ValueError:
    gi.require_version("WebKit2", "4.0")
from gi.repository import Gtk, WebKit2' 2>/dev/null || missing+=(webkit)
if [ ! -x "$VENV/bin/python" ]; then
    "$PY" -c 'import venv, ensurepip' 2>/dev/null || missing+=(venv)
fi
# the same lookup the audio library performs when it starts
"$PY" -c 'import ctypes.util, sys; sys.exit(0 if ctypes.util.find_library("portaudio") else 1)' \
    || missing+=(portaudio)

if [ "${#missing[@]}" -gt 0 ]; then
    pkgs=()
    if command -v apt-get >/dev/null 2>&1; then
        for m in "${missing[@]}"; do
            case "$m" in webkit) pkgs+=(python3-gi gir1.2-gtk-3.0 gir1.2-webkit2-4.1) ;; venv) pkgs+=(python3-venv) ;; portaudio) pkgs+=(libportaudio2) ;; esac
        done
        cmd=(sudo apt-get install -y "${pkgs[@]}")
    elif command -v dnf >/dev/null 2>&1; then
        for m in "${missing[@]}"; do
            case "$m" in webkit) pkgs+=(python3-gobject gtk3 webkit2gtk4.1) ;; portaudio) pkgs+=(portaudio) ;; esac
        done
        cmd=(sudo dnf install -y "${pkgs[@]}")
    elif command -v pacman >/dev/null 2>&1; then
        for m in "${missing[@]}"; do
            case "$m" in webkit) pkgs+=(python-gobject gtk3 webkit2gtk-4.1) ;; portaudio) pkgs+=(portaudio) ;; esac
        done
        cmd=(sudo pacman -S --needed --noconfirm "${pkgs[@]}")
    else
        say "Telsiz needs these system parts: ${missing[*]} (GTK and WebKit with their Python bindings, the Python venv module, the PortAudio library)."
        say "Install them with your package manager, then run this again."
        exit 1
    fi
    say "Telsiz needs: ${pkgs[*]}"
    say "Command:      ${cmd[*]}"
    if [ -t 0 ]; then
        read -r -p "Install them now? [Y/n] " answer
        case "${answer:-y}" in
            [Yy]*) "${cmd[@]}" ;;
            *) say "Run the command above yourself, then run this again."; exit 1 ;;
        esac
    else
        say "Run the command above in a terminal, then run this again."
        exit 1
    fi
fi

# ---------- private Python environment ----------
if [ ! -x "$VENV/bin/python" ]; then
    say "Setting up Telsiz for the first time, this takes a minute..."
    "$PY" -m venv --system-site-packages "$VENV"
elif grep -q '^include-system-site-packages = false' "$VENV/pyvenv.cfg" 2>/dev/null; then
    # made by Telsiz 1.4 or older: let it see the system's GTK bindings now
    sed -i 's/^include-system-site-packages = false/include-system-site-packages = true/' "$VENV/pyvenv.cfg"
fi
if [ "$(cat "$VENV/telsiz-deps" 2>/dev/null || true)" != "$STAMP_TEXT" ]; then
    say "Installing the libraries Telsiz needs..."
    specs=()
    for ((i = 0; i < ${#DEPS[@]}; i += 2)); do specs+=("${DEPS[i]}${DEPS[i + 1]}"); done
    "$VENV/bin/python" -m pip install --disable-pip-version-check -q "${specs[@]}"
    "$VENV/bin/python" -m pip install --disable-pip-version-check -q --no-deps "$PYNPUT"
    printf '%s' "$STAMP_TEXT" > "$VENV/telsiz-deps"
fi

# ---------- applications menu entry ----------
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICON="$VENV/telsiz-icon.png"
if [ ! -f "$ICON" ]; then
    (cd "$HERE" && "$VENV/bin/python" -c 'import telsiz; telsiz.make_icon("idle", 256).save(".venv/telsiz-icon.png")') \
        >/dev/null 2>&1 || true
fi
mkdir -p "$APPS"
DESKTOP="$APPS/telsiz.desktop"
new_entry="[Desktop Entry]
Type=Application
Name=Telsiz
Comment=Voice chat for your group
Exec=bash \"$HERE/run_linux.sh\"
Icon=$ICON
Terminal=false
Categories=Network;Chat;
StartupWMClass=Telsiz
"
if [ "$(cat "$DESKTOP" 2>/dev/null || true)" != "$new_entry" ]; then
    first_entry=$([ -f "$DESKTOP" ] && echo no || echo yes)
    printf '%s' "$new_entry" > "$DESKTOP"
    [ "$first_entry" = yes ] && say "Added Telsiz to your applications menu."
fi

# ---------- start ----------
LOG="$VENV/telsiz.log"
cd "$HERE"
nohup "$VENV/bin/python" "$HERE/telsiz.py" > "$LOG" 2>&1 &
pid=$!
sleep 2
if kill -0 "$pid" 2>/dev/null; then
    say "Telsiz is running. (Messages, if any, go to $LOG)"
else
    rc=0
    wait "$pid" || rc=$?
    if [ "$rc" -eq 0 ]; then
        say "Telsiz was already running: its window has been brought to the front."
    else
        say "Telsiz stopped right after starting. Last messages:"
        tail -n 20 "$LOG"
        exit 1
    fi
fi
