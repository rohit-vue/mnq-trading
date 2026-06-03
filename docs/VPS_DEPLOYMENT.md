# VPS Deployment Guide (MNQ Bot + IBKR Gateway)

This guide is for running the trading bot on a Linux VPS (e.g. `trader@srv1357947`) so it keeps running after you close SSH, and for keeping IBKR Gateway stable.

Your screenshots show:
- **IB Gateway** connected (API: connected, Market data: OK)
- Project likely at `~/mnq-trading` (or similar)
- IB Gateway installed at `~/IBGateway` with `ibgateway` launcher

---

## Part 1 — One-time VPS setup

### 1.1 Python environment

```bash
cd ~/mnq-trading   # adjust to your clone path
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 1.2 Environment file

Copy `.env.example` to `.env` and set:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `IB_CLIENT_ID=2` (use a **unique** client ID if anything else connects to IB)

### 1.3 IBKR config (important on VPS)

Edit `config/ibkr.yaml`:

```yaml
connection:
  host: "127.0.0.1"
  default_mode: "paper"      # or "live"
  default_gateway: "gateway" # NOT "tws" — you are using IB Gateway
  client_id: 2               # unique per app
```

Ports when using Gateway:
- Paper: **4002**
- Live: **4001**

TWS ports (7497/7496) only apply if you run TWS instead of Gateway.

### 1.4 Test manually once

```bash
cd ~/mnq-trading
source venv/bin/activate
python main_v2.py
```

Choose **2** for Paper Trade. Confirm Telegram + dashboard work, then stop with Ctrl+C.

---

## Part 2 — Run the bot as a background service (systemd)

`main_v2.py` uses an interactive menu. For systemd, auto-select Paper mode (option 2).

### 2.1 Create a small runner script

Use the repo script `deploy/run_paper.sh` (or copy it to your VPS):

```bash
chmod +x ~/mnq-trading/deploy/run_paper.sh
```

It runs the same flow as interactive mode:

```bash
python main_v2.py --mode paper --contracts 1
```

(equivalent to menu **2** + Enter on **Number of contracts [1]**)

Override contract count: `MNQ_CONTRACTS=2 ./deploy/run_paper.sh`

### 2.1b Manual commands (Windows or SSH)

Interactive (what you use locally):

```bash
py main_v2.py
# then: 2 → Enter for 1 contract
```

Non-interactive (VPS):

```bash
py main_v2.py --mode paper --contracts 1
```

### 2.1c Live trading (optional)

```bash
python main_v2.py --mode live --contracts 1
```

(Live still requires typing `CONFIRM LIVE TRADING` unless you start it interactively.)

### 2.2 systemd unit for the trading bot

Create `/etc/systemd/system/mnq-trading.service` (as root):

```ini
[Unit]
Description=MNQ SuperTrend Paper Trading Bot
After=network-online.target
Wants=network-online.target
# Start after IB Gateway is up (if you use ibgateway.service below)
After=ibgateway.service
Requires=ibgateway.service

[Service]
Type=simple
User=trader
Group=trader
WorkingDirectory=/home/trader/mnq-trading
ExecStart=/home/trader/mnq-trading/scripts/run_paper.sh
Restart=always
RestartSec=30
# Give bot time to shut down cleanly on stop
TimeoutStopSec=120

# Logs
StandardOutput=append:/home/trader/mnq-trading/logs/bot.stdout.log
StandardError=append:/home/trader/mnq-trading/logs/bot.stderr.log

[Install]
WantedBy=multi-user.target
```

```bash
mkdir -p ~/mnq-trading/logs
sudo systemctl daemon-reload
sudo systemctl enable mnq-trading.service
sudo systemctl start mnq-trading.service
```

### 2.3 Useful commands

| Command | Purpose |
|---------|---------|
| `sudo systemctl status mnq-trading` | Is the bot running? |
| `sudo journalctl -u mnq-trading -f` | Live logs (if using journal) |
| `tail -f ~/mnq-trading/logs/bot.stderr.log` | Error log |
| `tail -f ~/mnq-trading/trading.log` | Strategy log |
| `sudo systemctl restart mnq-trading` | Restart bot |
| `sudo systemctl stop mnq-trading` | Stop bot |

After SSH disconnect, the bot **keeps running** because systemd manages it.

---

## Part 3 — Keep IBKR Gateway always running

### 3.1 What you cannot disable

IBKR **requires** IB Gateway to restart **once per day**. You cannot turn this off permanently.  
Your bot already has **infinite reconnect** (`reconnection.max_attempts: 0` in `config/ibkr.yaml`).

What you *can* do: make Gateway **auto-restart and auto-login** so downtime is only 1–3 minutes.

### 3.2 IB Gateway GUI settings (do this on the VPS desktop)

Open **IB Gateway → Configure → Settings → Lock and Exit**:

1. **Auto restart** — enable, set time to **17:00 US/Eastern** (CME daily maintenance).
2. **Auto logon** — enable so Gateway logs back in after restart without you.
3. **Minimize to tray** / avoid closing the window manually.

Also in **Configure → API → Settings**:

- Enable **ActiveX and Socket Clients**
- **Read-Only API** = off (if you need orders)
- Trusted IP: `127.0.0.1`
- Socket port: **4002** (paper) or **4001** (live) — must match `config/ibkr.yaml`

Enable **delayed market data** in Gateway if you do not have a live CME subscription.

### 3.3 Run IB Gateway as a systemd service (headless-ish)

Your install has `~/IBGateway/ibgateway`. Example unit `/etc/systemd/system/ibgateway.service`:

```ini
[Unit]
Description=Interactive Brokers Gateway
After=network-online.target graphical.target
Wants=network-online.target

[Service]
Type=simple
User=trader
Group=trader
WorkingDirectory=/home/trader/IBGateway
Environment=DISPLAY=:0
# If you use Xvfb for headless: DISPLAY=:99 and start Xvfb separately
ExecStart=/home/trader/IBGateway/ibgateway
Restart=always
RestartSec=60

[Install]
WantedBy=multi-user.target
```

**Note:** IB Gateway is a Java GUI app. On a VPS you typically need:
- **Remote desktop** (what you use now), or
- **Xvfb** (virtual display) + optional **IBC** (IB Controller) for automated login

If Gateway only runs inside your RDP session, it **stops when that session ends** unless you use systemd + DISPLAY or IBC.

### 3.4 IB Controller (IBC) — recommended for 24/7

For production VPS, many traders use **[IBC](https://github.com/IbcAlpha/IBC)** to:
- Start Gateway automatically
- Handle daily restart window
- Auto-fill credentials (stored securely on the server)
- Recover from login dialogs

High-level flow:
1. Install IBC alongside `~/IBGateway`
2. Configure `config.ini` with credentials + trading mode (paper/live)
3. systemd runs IBC instead of raw `ibgateway`

### 3.5 Weekly manual login

IBKR often requires a **manual login about once per week** (security token refresh).  
When that happens:
- Gateway shows a login screen on the VPS desktop
- Log in via Remote Desktop
- Bot reconnects automatically after API is back

Telegram will alert on extended disconnects.

### 3.6 Prevent VPS sleep / reboot issues

```bash
# Disable suspend (example for systemd-logind)
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
```

Ensure the VPS provider does not force-reboot your VM without notice.

---

## Part 4 — Startup order

Correct order:

1. **IB Gateway** (API connected, market data OK)
2. **MNQ bot** (`mnq-trading.service`)

If the bot starts first, it will retry until Gateway is up (by design).

---

## Part 5 — Reading your Telegram status fields

From your hourly updates:

| Field | Meaning |
|-------|---------|
| **Buffered bars** | Number of candles loaded in memory for indicators |
| **Last bar age** | Minutes since the latest bar — should stay low during market hours |
| **Position: FLAT** | No open trade |
| **Session: CLOSED** | Outside futures trading hours (e.g. daily 17:00–18:00 ET maintenance) |

When **Last bar age** grows (e.g. 53 min) and session is **CLOSED**, that is normal — no new bars until the market reopens.

---

## Part 6 — Troubleshooting

| Symptom | Check |
|---------|--------|
| Bot exits immediately | `logs/bot.stderr.log`, menu input in `run_paper.sh` |
| API not connected | Gateway running? Port 4002? `default_gateway: gateway` |
| Historical farm inactive | Often OK until bot requests history; watch **last bar age** |
| No Telegram | `.env` tokens, `telegram.enabled: true` |
| Client ID conflict | Change `client_id` / `IB_CLIENT_ID` |
| Gateway dies after RDP disconnect | Use systemd + DISPLAY or IBC, not only manual GUI |

---

## Quick checklist

- [ ] `config/ibkr.yaml` → `default_gateway: gateway`, correct port
- [ ] `.env` filled on VPS
- [ ] `run_paper.sh` + `mnq-trading.service` enabled
- [ ] IB Gateway auto-restart + auto-logon at 17:00 ET
- [ ] API enabled, port 4002, 127.0.0.1 trusted
- [ ] Test: close SSH, `systemctl status mnq-trading` still active

Non-interactive CLI is supported: `python main_v2.py --mode paper --contracts 1`.
