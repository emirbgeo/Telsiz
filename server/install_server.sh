#!/bin/sh
# Installs (or updates) the Telsiz server as a background service on Ubuntu or Debian.
#
#   sudo sh install_server.sh          use UDP port 50505
#   sudo sh install_server.sh 40000    use another port (only on the first install)
#
# Run it from the folder that holds telsiz_server.py. Running it again later
# updates the program and keeps your passwords and bans.
set -eu

PORT="${1:-50505}"
case "$PORT" in
    ''|*[!0-9]*) echo "The port must be a number." >&2; exit 1 ;;
esac
if [ "$(id -u)" -ne 0 ]; then
    echo "Please run as root: sudo sh install_server.sh" >&2
    exit 1
fi

HERE=$(cd "$(dirname "$0")" && pwd)
# scp -r into an existing folder puts the new copy inside it as "server": use that copy
if [ -f "$HERE/server/install_server.sh" ] && [ -f "$HERE/server/telsiz_server.py" ]; then
    echo "Found a newer upload in $HERE/server, installing that one."
    exec sh "$HERE/server/install_server.sh" "$@"
fi
if [ ! -f "$HERE/telsiz_server.py" ]; then
    echo "telsiz_server.py must be in the same folder as this script." >&2
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    apt-get update
    apt-get install -y python3
fi
PY=$(command -v python3)
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)'; then
    echo "Python 3.8 or newer is needed." >&2
    exit 1
fi

# A dedicated user with no login and no home: the server never runs as root.
if ! id telsiz >/dev/null 2>&1; then
    useradd --system --no-create-home --home-dir /var/lib/telsiz --shell /usr/sbin/nologin telsiz
fi
install -d -m 755 /opt/telsiz
install -m 644 "$HERE/telsiz_server.py" /opt/telsiz/telsiz_server.py
install -d -o telsiz -g telsiz -m 700 /var/lib/telsiz

CONF=/var/lib/telsiz/server.json
if [ ! -f "$CONF" ]; then
    runuser -u telsiz -- "$PY" /opt/telsiz/telsiz_server.py --config "$CONF" --init --port "$PORT" >/dev/null
fi
PORT=$("$PY" -c 'import json, sys; print(int(json.load(open(sys.argv[1])).get("port", 50505)))' "$CONF")

cat > /etc/systemd/system/telsiz.service <<EOF
[Unit]
Description=Telsiz voice chat server
After=network-online.target
Wants=network-online.target

[Service]
User=telsiz
Group=telsiz
ExecStart=$PY -u /opt/telsiz/telsiz_server.py --config $CONF
Restart=always
RestartSec=3
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ReadWritePaths=/var/lib/telsiz

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable telsiz >/dev/null 2>&1
systemctl restart telsiz

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
    ufw allow "$PORT"/udp >/dev/null
    echo "Firewall (ufw): opened UDP port $PORT."
fi

VER=$(sed -n 's/^SERVER_VERSION = "\(.*\)"/\1/p' /opt/telsiz/telsiz_server.py)
echo
echo "Telsiz server $VER is running."
"$PY" - "$CONF" <<'EOF'
import json, sys
c = json.load(open(sys.argv[1]))
print("  UDP port:       %s" % c.get("port"))
print("  room password:  %s   (give this to friends)" % c.get("password"))
print("  admin password: %s   (keep this for yourself: it unlocks Kick and Ban)" % c.get("admin_password"))
EOF
echo
echo "If your provider has its own firewall panel, allow inbound UDP $PORT there too."
echo "Live log:        journalctl -u telsiz -f"
echo "Passwords later: sudo cat $CONF"
