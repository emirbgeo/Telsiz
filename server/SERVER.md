# Running a Telsiz server

A Telsiz server is a room that lives on a rented Linux machine (a VPS) instead of your PC. Everyone, you included, uses "Join a room" in the normal Telsiz app. Your home address is no longer involved at all: friends and viewers only ever see the server's address.

The server only passes voice packets along. It decodes nothing, needs no sound card and uses only Python's standard library, so the smallest plan any provider sells is far more than enough.

## 1. Rent a server

Pick Ubuntu 24.04 (Debian 12 also works), the smallest plan, and a region close to your friends.

| Provider | Region | Smallest plan (checked October 2026) |
|---|---|---|
| DigitalOcean | Frankfurt | $4/month: 1 vCPU, 512 MiB RAM, 500 GiB transfer |
| Hetzner | Germany or Finland | from €5.99/month excl. VAT, plus €0.50/month for the IPv4 address |
| is*hosting | Istanbul | from $5.94/month: 1 GB RAM, 1 TB bandwidth |

Frankfurt is about 25 ms from Istanbul between data centers, which you won't hear in voice. An Istanbul server is closer still if your friends are all in Turkey.

When you create the server, add your SSH key if the panel offers it (safer than a password), and note the server's IP address.

## 2. Install

On your Windows PC, open PowerShell in the folder that holds this `server` folder (Windows 10 and 11 include `ssh` and `scp`):

```
scp -r server root@SERVER_IP:/root/telsiz-server
ssh root@SERVER_IP
sudo sh /root/telsiz-server/install_server.sh
```

If your provider gives you a user such as `ubuntu` instead of `root`, use that name; `sudo` takes care of the rest.

The installer:
* creates a user called `telsiz` with no login, so the server never runs as root;
* puts the program in `/opt/telsiz` and the settings in `/var/lib/telsiz/server.json`;
* starts it as a service that starts again by itself after a crash or reboot;
* opens the UDP port in `ufw` if that firewall is switched on;
* prints the room password and the admin password. Write both down.

If you added a firewall in the provider's web panel, allow inbound UDP 50505 there.

## 3. Join

In Telsiz choose "Join a room":
* Address: `SERVER_IP:50505`
* Password: the **admin password** for you. It works as a room password and also gives you Kick, Ban and Bans.
* Your friends use the **room password**. Never give out the admin password.

Once everyone is on the server, **delete the UDP 50505 forwarding rule on your router.** From then on nothing at your home address answers anyone.

## Everyday tasks

| Task | Command (on the server) |
|---|---|
| See who joins, leaves, gets kicked | `journalctl -u telsiz -f` |
| Show the passwords | `sudo cat /var/lib/telsiz/server.json` |
| Change a password | `sudo nano /var/lib/telsiz/server.json`, then `sudo systemctl restart telsiz` |
| List bans | `sudo runuser -u telsiz -- python3 /opt/telsiz/telsiz_server.py --config /var/lib/telsiz/server.json --list-bans` |
| Remove ban number 3 | same command with `--unban 3` instead of `--list-bans` (works while the room runs) |
| Update Telsiz server | see "Updating" below; passwords, bans and rooms stay |
| Keep the system patched | `sudo apt update && sudo apt upgrade` about once a month |
| Remove everything | `sudo systemctl disable --now telsiz`, then `sudo rm -rf /opt/telsiz /var/lib/telsiz /etc/systemd/system/telsiz.service` and `sudo userdel telsiz` |

## Updating

**From GitHub (simplest).** On the server:

```
ssh root@SERVER_IP
rm -rf /root/telsiz-src && mkdir /root/telsiz-src
curl -fsSL https://github.com/emirbgeo/Telsiz/archive/refs/heads/main.tar.gz | tar xz -C /root/telsiz-src --strip-components=1
sh /root/telsiz-src/server/install_server.sh
```

That downloads the newest code from the `main` branch. For a specific release use `archive/refs/tags/v1.6.tar.gz` (with the version you want) instead of `archive/refs/heads/main.tar.gz`.

**From your PC.** From PowerShell in the unzipped `telsiz` folder:

```
ssh root@SERVER_IP "rm -rf /root/telsiz-server"
scp -r server root@SERVER_IP:/root/telsiz-server
ssh root@SERVER_IP "sh /root/telsiz-server/install_server.sh"
```

The first line matters: when the folder already exists, `scp -r` puts the new copy inside it as `server` instead of replacing it, and the old installer would reinstall the old version. (Installers from 1.4 on notice this and use the inner copy.)

Either way the installer ends with the version now running, for example `Telsiz server 1.4 is running.` Only the download or upload folder is replaced: your passwords, bans and rooms live in `/var/lib/telsiz` and stay. Never delete `/var/lib/telsiz` unless you want to start over with new passwords.

## How the room behaves

* **Kick** keeps that person out until everyone has left the room or the server restarts (a server runs for weeks, so "until the host restarts" from PC hosting would mean almost forever).
* **Ban** lasts until an admin unbans them, from the app's Bans window or with `--unban`. It covers their device and their Internet address, exactly like PC hosting.
* Admins can't kick or ban each other.
* After 10 wrong passwords from one address within a minute, that address is ignored for the rest of the minute, so passwords can't be guessed quickly. Replies to strangers are rate limited, so the server can't be used to flood anyone.
* Friends still on Telsiz 1.1 can join and talk. Only the admin needs 1.2 for Kick and Ban.

## Mute and deafen marks (server 1.4)

* Apps 1.4 and newer report when they mute or deafen; the server passes everyone's state to every room, so names show `[muted]` or `[deafened]`. Older apps keep working and simply show no mark.

## Rooms and screen sharing (server 1.3)

* Everyone starts in the **Lobby**. Admins create and delete rooms from the app (New room, Delete room). Rooms are saved in `/var/lib/telsiz/telsiz_rooms.json` and survive restarts; deleting a room moves its people to the Lobby. Up to 25 rooms.
* Voice stays inside a room. Everyone sees all rooms and who is in them.
* One person per room can share a screen. The server sends the picture only to people in that room who pressed Watch, and keeps the last few seconds of it so a viewer who lost packets gets them again from the server instead of waiting for a new full picture.
* Apps older than 1.3 can still join and talk; they stay in the Lobby.
* Updating an existing server: see "Updating" above.

## Bandwidth

Each person talking costs about 45 kbit/s for every listener. The server sends that to everyone else, so 6 people with one or two talking at a time is roughly 0.2 to 0.5 Mbit/s. Three hours every evening for a month is around 15 GB, a few percent of the smallest plans above.

Screen sharing costs at most 1.5 Mbit/s per viewer (measured 1.0 to 1.45 Mbit/s for a busy screen). Five people watching is at most 7.5 Mbit/s, about 3.4 GB per hour. A 1 TB plan therefore covers roughly 290 hours of five-viewer sharing a month on top of voice, and a 1 Gbit/s port is never the limit. Relaying it used 0.5% of one CPU core in testing.

## Limits

* No encryption, same as PC hosting. That includes the admin password, which crosses the internet each time you join. Don't join as admin from public Wi Fi.
* Up to 50 people per server (`max_clients` in the settings file; 15 by default).
