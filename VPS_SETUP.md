# VPS Setup Guide — Running the PyQuant Orchestra Process

`p4_orchestra.py` is a **long-running process** (an infinite loop with a
`schedule` scheduler inside it — see its own file) that has to stay alive
24/7, reconnecting to Binance/Alpaca and placing real orders on its own
schedule. That is fundamentally different from `api/index.py` (a
stateless request/response API, which is why that one belongs on Vercel)
— it needs an always-on machine, which is what a VPS (Virtual Private
Server) is for.

## Recommended provider: DigitalOcean

**Recommendation: DigitalOcean**, specifically a **Basic Droplet, Regular
(shared CPU), 2 GB RAM / 1 vCPU / 50 GB SSD** (currently ~$12/month).
Reasoning:

- The trading stack (`numpy`, `pandas`, `scikit-learn`, `filterpy`,
  `optuna`) is memory-hungrier than a typical small web app, especially
  during `daily_optimizer()`'s Bayesian search — 1 GB droplets ($6/mo)
  risk the OOM killer during that window; 2 GB gives real headroom.
- DigitalOcean's control panel and documentation are unusually
  beginner-friendly for exactly this "spin up one always-on Linux box"
  use case, with first-party guides for the systemd steps below.
- Region doesn't meaningfully affect this bot — it trades on hourly/
  15-minute/1-minute cycles, not sub-second HFT, so sub-50ms latency to
  Binance isn't a real constraint. Pick whichever region is closest to
  you (for easier SSH/dashboard latency); **Singapore (SGP1)** is the
  closest DigitalOcean region to Binance's own infrastructure if you'd
  like to optimize for that anyway.

(A cheaper alternative if budget is the priority: **Hetzner Cloud**'s
CX22 plan is similarly specced for roughly half the price, but has a
less beginner-friendly signup/verification process and no first-party
hand-holding docs as detailed as DigitalOcean's — the steps below are
DigitalOcean-specific.)

---

## Part A — Create your DigitalOcean account

1. Go to https://www.digitalocean.com in your browser.
2. Click **Sign up** (top-right).
3. Enter your email and a password (or sign up with Google/GitHub), then
   follow the verification email link DigitalOcean sends you.
4. On first login, DigitalOcean requires a billing method before you can
   create anything: go to **Billing** in the left sidebar (or you'll be
   prompted automatically) and add a credit/debit card or PayPal.

---

## Part B — Generate an SSH key (on your Windows machine)

You'll use this to log into the server securely instead of a password.

1. Open PowerShell (not Command Prompt — PowerShell has `ssh-keygen`
   built in on modern Windows 10/11).
2. Run:
   ```
   ssh-keygen -t ed25519 -C "pyquant-vps"
   ```
3. When prompted `Enter file in which to save the key`, press Enter to
   accept the default location (`C:\Users\Pyrex\.ssh\id_ed25519`).
4. When prompted for a passphrase, you can press Enter twice to leave it
   empty (simpler for an automated server login) or set one if you
   prefer the extra protection — just remember it if you do.
5. This creates two files: `id_ed25519` (private — never share this) and
   `id_ed25519.pub` (public — this is what you upload to DigitalOcean).
6. Print the public key so you can copy it:
   ```
   type C:\Users\Pyrex\.ssh\id_ed25519.pub
   ```
   Select and copy the entire output line (starts with `ssh-ed25519`).

---

## Part C — Create the Droplet

1. Log into https://cloud.digitalocean.com.
2. Click the green **Create** button (top of the page) → **Droplets**.
3. **Choose Region**: pick the one closest to you, or `SGP1` (Singapore)
   per the reasoning above. Leave the VPC network at its default.
4. **Choose an image**: click the **OS** tab, select **Ubuntu**, and
   pick **24.04 (LTS) x64** from the version dropdown.
5. **Choose Size**: under **Droplet Plan**, pick **Basic**, then under
   **CPU options** pick **Regular (Shared CPU)**, then select the
   **$12/mo — 2 GB RAM / 1 vCPU / 50 GB SSD** row.
6. Skip **Add Volume** and **Enable automated backups** (optional, extra
   cost — safe to skip for now; the state that matters is in Supabase,
   not on this disk).
7. **Choose Authentication Method**: select **SSH Key**, click
   **Add SSH Key**, paste the public key you copied in Part B into the
   text box, give it a name like `pyquant-vps-key`, click **Add SSH Key**.
   Then make sure the checkbox next to that newly-added key is ticked.
8. **Finalize Details**: under **Hostname**, type something recognizable
   like `pyquant-orchestra`. Leave Tags/Project as default.
9. Click **Create Droplet**.
10. Wait ~60 seconds for the progress bar to finish. Once done, note the
    **IP address** shown next to your new Droplet in the dashboard —
    you'll need it for every step below. This guide refers to it as
    `YOUR_SERVER_IP`.

---

## Part D — First login and basic hardening

1. From PowerShell on your Windows machine:
   ```
   ssh root@YOUR_SERVER_IP
   ```
   Type `yes` if asked about the host's fingerprint. You should land at
   a root shell prompt (`root@pyquant-orchestra:~#`) with no password
   needed — the SSH key is already authorizing you.

2. Update the system packages:
   ```
   apt update && apt upgrade -y
   ```

3. Create a non-root user to run the bot under (running a 24/7 internet-
   facing process as `root` is unnecessary risk):
   ```
   adduser pyquant
   ```
   It will ask for a password — set one and remember it (you'll rarely
   use it, but `sudo` will ask for it). Press Enter through the
   Full Name/Room Number/etc prompts (optional, can be left blank), then
   type `Y` to confirm.

4. Give that user `sudo` rights and copy your SSH key to it:
   ```
   usermod -aG sudo pyquant
   rsync --archive --chown=pyquant:pyquant ~/.ssh /home/pyquant
   ```

5. Set up the firewall — this bot makes outbound connections to Binance/
   Alpaca/Telegram/Supabase but never needs to *accept* inbound
   connections except SSH:
   ```
   ufw allow OpenSSH
   ufw enable
   ```
   Type `y` to confirm when prompted.

6. Log out of the root session and log back in as `pyquant` to confirm
   it works:
   ```
   exit
   ```
   ```
   ssh pyquant@YOUR_SERVER_IP
   ```
   You should land at `pyquant@pyquant-orchestra:~$` with no password
   prompt (the SSH key copied over in step 4 is authorizing you). From
   here on, every command below assumes you're logged in as `pyquant`.

---

## Part E — Install Python and system dependencies

```
sudo apt install -y python3 python3-venv python3-pip git build-essential
```

Confirm Python's version (needs to be 3.11+, matching `requirements.txt`'s
header):
```
python3 --version
```

---

## Part F — Get the code onto the server

Since Claude's GitHub App isn't currently authorized to push to
`Pyrex611/pyq-p3` (a separate, already-reported blocker — unrelated to
this VPS setup), the simplest path is transferring a zip directly from
your Windows machine rather than `git clone`.

### F1. From your Windows machine, zip the project

In PowerShell, from the parent folder of your repo:
```
cd C:\Users\Pyrex
Compress-Archive -Path pyq-p3 -DestinationPath pyq-p3.zip -Force
```

### F2. Copy it to the server

Still in PowerShell:
```
scp C:\Users\Pyrex\pyq-p3.zip pyquant@YOUR_SERVER_IP:/home/pyquant/
```
Enter the `pyquant` user's password if prompted (SSH keys authorize
interactive logins; `scp` may still ask once depending on your OpenSSH
version — if it doesn't ask, it just works).

### F3. Unzip it on the server

Back in your SSH session (`ssh pyquant@YOUR_SERVER_IP`):
```
sudo apt install -y unzip
cd /home/pyquant
unzip pyq-p3.zip
cd pyq-p3
```

(For future updates, repeat F1–F3 — re-zip, `scp` again, and `unzip -o
pyq-p3.zip` to overwrite. This is manual but reliable; once the GitHub
push block is resolved, `git pull` on the server becomes the faster
path.)

---

## Part G — Python environment and dependencies

From `/home/pyquant/pyq-p3`:

```
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

This will take a few minutes (numpy/pandas/scikit-learn/optuna are large
packages). You should see `Successfully installed ...` at the end with
no red error text.

---

## Part H — Configure `.env`

```
nano .env
```

Paste in your actual credentials, following the same variable names the
code already expects (check `p4_utils.py`'s top-of-file `os.environ.get`
calls, or your existing local `.env`, for the exact names) — at minimum:

```
BINANCE_API_KEY=...
BINANCE_SECRET_KEY=...
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
TELEGRAM_TOKEN=...
TELEGRAM_CHAT_ID=...
DATABASE_URL=postgresql://postgres:YOUR_PASSWORD@YOUR_PROJECT_REF.supabase.co:5432/postgres
PYQUANT_MODE=live
```

Save and exit nano: press `Ctrl+O`, Enter (to confirm the filename),
then `Ctrl+X` (to exit).

`DATABASE_URL` here should be the same **direct connection** (port
`5432`) string from your Supabase project — this is the one correct
place for it, since this is the single long-running process that holds
a persistent connection (see `SUPABASE_SETUP.md`). This is the same value
you also set on Vercel for the read-only API — same database, two
different processes reading/writing it.

---

## Part I — Run it as a systemd service (so it survives reboots/crashes)

Create the service file:
```
sudo nano /etc/systemd/system/pyquant-orchestra.service
```

Paste in exactly this (adjust nothing except confirming the paths match
where you unzipped the project):

```ini
[Unit]
Description=PyQuant Orchestra (trading scheduler)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pyquant
WorkingDirectory=/home/pyquant/pyq-p3
EnvironmentFile=/home/pyquant/pyq-p3/.env
ExecStart=/home/pyquant/pyq-p3/venv/bin/python3 p4_orchestra.py
Restart=always
RestartSec=10
StandardOutput=append:/home/pyquant/pyq-p3/orchestra.log
StandardError=append:/home/pyquant/pyq-p3/orchestra.log

[Install]
WantedBy=multi-user.target
```

Save and exit (`Ctrl+O`, Enter, `Ctrl+X`).

Enable and start it:
```
sudo systemctl daemon-reload
sudo systemctl enable pyquant-orchestra
sudo systemctl start pyquant-orchestra
```

`enable` makes it auto-start on every server reboot; `Restart=always`
makes systemd restart it automatically if the process ever crashes.

---

## Part J — Verify it's running

Check the service status:
```
sudo systemctl status pyquant-orchestra
```
You should see `Active: active (running)` in green.

Watch the live logs:
```
tail -f /home/pyquant/pyq-p3/orchestra.log
```
You should see the startup line
`[Orchestra] Scheduler running (pyq_p4). Press Ctrl-C to stop.` and,
within a minute, the first `one_minute_task` activity. Press `Ctrl+C` to
stop watching the log (this does **not** stop the service — it's `tail`
you're exiting, not the bot).

Also check via `journalctl` (systemd's own log, useful if `orchestra.log`
itself has a problem):
```
sudo journalctl -u pyquant-orchestra -f
```

---

## Common operations going forward

- **Restart after an update:** `sudo systemctl restart pyquant-orchestra`
- **Stop it:** `sudo systemctl stop pyquant-orchestra`
- **Switch LIVE ↔ PAPER:** edit `.env`'s `PYQUANT_MODE` line, then
  `sudo systemctl restart pyquant-orchestra` (per `DOCS.md` Section 6,
  this one setting is restart-only by design — it is not the UI's OFF
  switch, which works without a restart).
- **Check disk space** (logs grow over time):
  `df -h` and `du -sh /home/pyquant/pyq-p3/orchestra.log` — rotate/trim
  the log file manually if it grows large, e.g.
  `sudo truncate -s 0 /home/pyquant/pyq-p3/orchestra.log` (safe to do any
  time; it just empties the file, the service keeps appending to it).
