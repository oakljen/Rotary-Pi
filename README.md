# Rotary-Pi

A Raspberry Pi Zero 2W project that converts a vintage GPO 746 rotary phone into a working SIP extension, registered on a self-hosted Asterisk PBX.

---

## Features

- Rotary dial pulse detection via GPIO
- Hook switch detection (lift to dial, replace to hang up)
- Electromagnetic bell driver via L298N H-bridge
- SIP calling via [baresip](https://github.com/baresip/baresip)
- Spoken digit confirmation via espeak-ng ("one", "two" …)
- Audible call failure feedback ("number not found", "number busy" etc.)
- Australian 400 Hz dial tone while waiting for digits
- Simulation mode when run without GPIO (for dev/testing on non-Pi hardware)

---

## Hardware

| Component | Details |
|---|---|
| Pi | Raspberry Pi Zero 2W |
| Phone | GPO 746 (rotary) |
| Audio | USB audio dongle (plughw:1,0) |
| Bell driver | L298N H-bridge module |
| Power | USB-C PD power bank + capacitor buffer on bell supply |

### GPIO pins (BCM)

| Pin | Function |
|---|---|
| 3 | Hook switch (HIGH = on hook, LOW = lifted) |
| 18 | Rotary dial pulse (FALLING edge = one pulse) |
| 14 | Bell H-bridge IN2 |
| 15 | Bell H-bridge IN1 |

---

## Quick start

```bash
git clone https://github.com/oakljen/Rotary-Pi.git ~/rotary-pi
cd ~/rotary-pi
bash setup.sh
```

`setup.sh` installs all dependencies, sets up the systemd service, and configures the 5-minute auto-update cron job. You'll be prompted to enter your SIP credentials.

---

## Manual setup

**1. Install dependencies**
```bash
sudo apt update
sudo apt install -y baresip sox espeak-ng python3-pip git
pip3 install python-dotenv --break-system-packages
```

**2. Configure credentials**
```bash
cp .env.example .env
nano .env
```

Fill in your SIP server, extension, and password. To find your ALSA audio device:
```bash
aplay -l
```
Set `AUDIO_DEVICE` to something like `alsa,plughw:1,0`.

**3. Run**
```bash
python3 rotary_phone_sip.py
```

---

## SIP / Asterisk

The phone registers as a SIP extension on a self-hosted Asterisk PBX. Example `sip.conf` entry:

```ini
[1002]
type=friend
secret=yourpassword
host=dynamic
context=internal
```

---

## How dialling works

1. Lift handset → 400 Hz dial tone plays
2. Dial digits — each committed digit is spoken aloud ("one", "two" …)
3. After 3 seconds of silence the call is placed automatically
4. Replace handset at any time to cancel or hang up
5. On failure (404, busy etc.) two descending tones play followed by a spoken reason

Pulse-to-digit mapping follows standard GPO convention: 1 pulse = 1, …, 9 pulses = 9, 10 pulses = 0.

---

## Auto-updates

`setup.sh` installs a cron job that pulls from GitHub every 5 minutes. It only restarts the service if files actually changed, and skips restart if a call is in progress.

To check the update log:
```bash
tail -f ~/rotary-pi/update.log
```

---

## Running as a service

`setup.sh` handles this automatically. To do it manually:

```bash
sudo nano /etc/systemd/system/rotary-phone.service
```

```ini
[Unit]
Description=Rotary Phone SIP Bridge
After=network.target sound.target

[Service]
ExecStart=/usr/bin/python3 /home/pi/rotary-pi/rotary_phone_sip.py
WorkingDirectory=/home/pi/rotary-pi
Restart=on-failure
RestartSec=5
User=pi

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now rotary-phone
```

---

## Project structure

```
Rotary-Pi/
├── rotary_phone_sip.py   # main script
├── setup.sh              # one-shot installer
├── .env.example          # credentials template (copy to .env)
├── .gitignore
└── README.md
```

---

## License

MIT
