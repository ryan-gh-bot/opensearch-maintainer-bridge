# Running the bridge as a systemd service

Two unit files are provided:

- **`opensearch-maintainer-bridge.user.service`** — user-level systemd unit. Runs as your Unix user, manageable without `sudo`. Requires **systemd 240 or newer** and the `user@.service` template that ships with it (present on modern Ubuntu / Debian / Fedora / Amazon Linux 2023). Works out of the box — `%h` expands to your home directory. **Preferred when it works.**

- **`opensearch-maintainer-bridge.system.service`** — system-level unit installed under `/etc/systemd/system/`, managed with `sudo`. Needed for older distros (e.g. Amazon Linux 2, which ships systemd 219 and has no user-service support). Requires you to edit two placeholders before installing.

Pick the one that matches your host, install it as described below, and you're done — the bridge starts on boot, auto-restarts on crash, and streams its logs into journald.

## Which one do I need?

```bash
systemctl --version | head -1     # your systemd version
```

- **240 or newer**, and `ls /usr/lib/systemd/system/user@.service` shows a file → use **user** variant.
- **Older than 240**, or `user@.service` is missing → use **system** variant.

If you're not sure, try the user variant first; if `systemctl --user daemon-reload` fails with "Failed to get D-Bus connection", fall back to the system variant.

---

## Option A: user service (systemd ≥ 240)

```bash
# 1. Copy the unit into your user systemd config dir.
mkdir -p ~/.config/systemd/user
cp contrib/systemd/opensearch-maintainer-bridge.user.service \
   ~/.config/systemd/user/opensearch-maintainer-bridge.service

# 2. Enable lingering so the user systemd manager keeps running across
#    reboots and after you log out. Needs one-time sudo.
sudo loginctl enable-linger $USER

# 3. Reload user systemd, then enable and start the service.
systemctl --user daemon-reload
systemctl --user enable --now opensearch-maintainer-bridge.service
```

Day-to-day management:

```bash
systemctl --user status  opensearch-maintainer-bridge     # is it up?
systemctl --user restart opensearch-maintainer-bridge     # config or code change
systemctl --user stop    opensearch-maintainer-bridge     # manual shutdown
journalctl  --user -u    opensearch-maintainer-bridge -f  # tail logs
```

---

## Option B: system service (systemd < 240, or Amazon Linux 2)

```bash
# 1. Copy the template, then edit two placeholders in the copy:
#    - User=<your unix user>
#    - Environment=HOME=/home/<your unix user>
#    - the three /home/<your user>/workplace/github-bridge paths
sudo cp contrib/systemd/opensearch-maintainer-bridge.system.service \
        /etc/systemd/system/opensearch-maintainer-bridge.service
sudo sed -i "s/REPLACE_WITH_YOUR_USER/$USER/g" \
        /etc/systemd/system/opensearch-maintainer-bridge.service

# 2. Reload systemd and start the service.
sudo systemctl daemon-reload
sudo systemctl enable --now opensearch-maintainer-bridge
```

Day-to-day management:

```bash
sudo systemctl status  opensearch-maintainer-bridge     # is it up?
sudo systemctl restart opensearch-maintainer-bridge     # config or code change
sudo systemctl stop    opensearch-maintainer-bridge     # manual shutdown
sudo journalctl -u     opensearch-maintainer-bridge -f  # tail logs
```

If your user is in the `systemd-journal` group (or `wheel` on some distros), `journalctl -u opensearch-maintainer-bridge` may work without `sudo`.

---

## Common tasks

**After editing `~/.config/opensearch-maintainer-bot/config.yaml`:**

```bash
# user variant:
systemctl --user restart opensearch-maintainer-bridge

# system variant:
sudo systemctl restart opensearch-maintainer-bridge
```

**After `git pull` on the bridge repo:**

Same restart command. The service invokes the checked-out `bridge.py`, so a `git pull` alone doesn't reload the running code — restart the unit.

**Checking the bridge is polling GitHub (not silently stuck):**

```bash
# user variant:
journalctl --user -u opensearch-maintainer-bridge --since "1 min ago" | grep "poll "

# system variant:
sudo journalctl -u opensearch-maintainer-bridge --since "1 min ago" | grep "poll "
```

You should see one `poll <repo>:` line per configured repo every 30 seconds (or whatever `poll_interval_seconds` is in your config).

**Uninstalling:**

```bash
# user variant:
systemctl --user disable --now opensearch-maintainer-bridge
rm ~/.config/systemd/user/opensearch-maintainer-bridge.service
sudo loginctl disable-linger $USER   # optional; remove only if no other user services

# system variant:
sudo systemctl disable --now opensearch-maintainer-bridge
sudo rm /etc/systemd/system/opensearch-maintainer-bridge.service
sudo systemctl daemon-reload
```

## Troubleshooting

**`[Error] Agent invocation failed: kiro-cli not found on PATH`** (posted as a
comment on GitHub, or seen in the journal).

systemd runs the service with a minimal `PATH`
(`/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin`) that does not include
your interactive shell's additions. The agent CLI (`kiro-cli`, installed by
Amazon's Toolbox under `~/.toolbox/bin`) isn't on that minimal PATH, so the
bridge can't launch it.

Both unit files here already set an `Environment=PATH=...` that prepends
`~/.toolbox/bin` (plus `~/.local/bin` and `~/bin`). If you installed an older
copy of the unit, or your agent CLI lives elsewhere, add a drop-in without
touching the main unit:

```bash
# system variant:
sudo mkdir -p /etc/systemd/system/opensearch-maintainer-bridge.service.d
sudo tee /etc/systemd/system/opensearch-maintainer-bridge.service.d/path.conf >/dev/null <<EOF
[Service]
Environment=PATH=$HOME/.toolbox/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
EOF
sudo systemctl daemon-reload
sudo systemctl restart opensearch-maintainer-bridge
```

Verify the running service's PATH:

```bash
sudo systemctl show opensearch-maintainer-bridge -p Environment
```

Find where your agent CLI actually is: `which kiro-cli` in an interactive shell.

## What the unit does NOT do

- **Does not create the workdir / venv / config.** You still need to complete the End-to-end setup steps in the top-level README before installing the service.
- **Does not manage the agent package.** The agent is installed separately via `aim agents install`. The bridge unit just runs `bridge.py`, which invokes whatever agent is currently installed.
- **Does not rotate the workdir's local git checkouts.** Those are managed by the agent workflow itself; the unit doesn't touch them.
