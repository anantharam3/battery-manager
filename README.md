# 🔋 Battery Manager

A production-grade Python daemon for protecting a Linux server battery using a Tuya smart plug, with full Telegram bot control. Built for a Dell Inspiron 7559 running as a 24/7 home server.

## Features

- **Daemon mode** — Keeps battery between configurable thresholds (default 45-90%) using smart plug ON/OFF
- **Calibration mode** — Automated 3-cycle full discharge-to-charge cycle to recalibrate the BMS fuel gauge
- **Telegram control** — Full remote control via Telegram bot (14 commands)
- **Survives reboots** — Calibration state persists to disk, resumes automatically on restart
- **Safe plug logic** — Never toggles plug on Tuya network errors (avoids false triggers)

---

## Hardware Tested On

- Dell Inspiron 7559 (i7-6700HQ, 16GB RAM)
- Ubuntu / Linux Mint, kernel 7.x
- Tuya Wi-Fi Smart Plug (local control, no cloud required)

---

## Prerequisites (on the server)

```bash
sudo apt install python3-pip python3-venv git -y
python3 -m venv ~/venv
~/venv/bin/pip install requests tinytuya python-dotenv
```

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/YOUR_USERNAME/battery-manager.git ~/battery_manager
cd ~/battery_manager
~/venv/bin/pip install -r requirements.txt
```

### 2. Configure secrets

```bash
cp .env.example ~/.env
nano ~/.env
```

Fill in your .env:

```
TUYA_DEVICE_ID=your_device_id
TUYA_IP_ADDRESS=192.168.1.x
TUYA_LOCAL_KEY=your_local_key

TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

### 3. Create the data directory

```bash
sudo mkdir -p /data
sudo chown $USER:$USER /data
```

### 4. Run connection tests

```bash
~/venv/bin/python3 battery_manager.py --test
```

All 5 tests should pass: battery read, plug OFF, plug ON, Telegram, system stats.

### 5. Start the daemon

```bash
nohup ~/venv/bin/python3 ~/battery_manager/battery_manager.py >> /data/battery_manager.log 2>&1 &
```

### 6. Auto-start on reboot

```bash
crontab -e
```

Add:
```
@reboot sleep 30 && nohup /home/YOUR_USER/venv/bin/python3 /home/YOUR_USER/battery_manager/battery_manager.py >> /data/battery_manager.log 2>&1 &
```

---

## Telegram Commands

| Command | Description |
|---------|-------------|
| /status | Full status: battery %, CPU temp, load, uptime, plug |
| /plug on | Force charger ON |
| /plug off | Force charger OFF |
| /set low N | Daemon LOW threshold % (default: 45) |
| /set high N | Daemon HIGH threshold % (default: 90) |
| /set alert N | Alert interval hours (default: 1) |
| /calibrate start | Start 3-cycle BMS calibration |
| /calibrate status | Show calibration progress |
| /ps | Top 8 processes by CPU |
| /stop | Gracefully stop the battery manager |
| /shutdown | Shutdown server (needs /confirm) |
| /reboot | Reboot server (needs /confirm) |
| /confirm | Confirm pending shutdown/reboot |
| /help | List all commands |

---

## Updating via Git

```bash
# On the server
cd ~/battery_manager
git pull
pkill -f battery_manager.py || true
sleep 3
rm -f /data/battery_manager.pid
nohup ~/venv/bin/python3 battery_manager.py >> /data/battery_manager.log 2>&1 &
sleep 5
tail -10 /data/battery_manager.log
```

Or from your dev machine:

```bash
bash deploy.sh ananth@192.168.1.14
```

---

## Monitoring

```bash
tail -f /data/battery_manager.log        # Live log
ps aux | grep battery_manager            # Check if running
cat /data/battery_state.json            # Calibration state
```

---

## Dell Inspiron 7559 Notes

- BIOS: Change Battery Charge Config from Adaptive to Standard (Advanced tab) to allow 100% charging
- Battery temp is NOT exposed via sysfs on this model — shows N/A
- at_full detection: treats Not charging + cap >= 85% + 0W draw as BMS Full (EC charge ceiling)

---

## License

MIT
