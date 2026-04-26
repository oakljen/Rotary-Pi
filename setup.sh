#!/usr/bin/env bash
# =============================================================================
# Rotary-Pi setup script
# =============================================================================
# Installs all dependencies, configures the .env file, sets up the systemd
# service, and adds a cron job to auto-pull updates from GitHub every 5 min.
#
# Usage:
#   bash setup.sh
# =============================================================================

set -e

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="rotary-phone"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
CURRENT_USER="$(whoami)"
PYTHON="$(which python3)"
SCRIPT="${REPO_DIR}/rotary_phone_sip.py"
UPDATE_SCRIPT="${REPO_DIR}/update.sh"
ENV_FILE="${REPO_DIR}/.env"
LOG_FILE="${REPO_DIR}/update.log"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()    { echo -e "${GREEN}[+]${NC} $*"; }
warn()    { echo -e "${YELLOW}[!]${NC} $*"; }
error()   { echo -e "${RED}[✗]${NC} $*"; exit 1; }
section() { echo -e "\n${GREEN}══ $* ══${NC}"; }

# ── Root check ────────────────────────────────────────────────────────────────
if [ "$EUID" -eq 0 ]; then
    error "Don't run this as root. Run as your normal user — sudo will be used where needed."
fi

echo ""
echo "  ╔═══════════════════════════════════════╗"
echo "  ║        Rotary-Pi  Setup               ║"
echo "  ╚═══════════════════════════════════════╝"
echo "  Repo : ${REPO_DIR}"
echo "  User : ${CURRENT_USER}"
echo ""

# ── 1. System packages ────────────────────────────────────────────────────────
section "Installing system packages"
sudo apt-get update -qq
sudo apt-get install -y baresip sox espeak-ng python3-pip git
info "System packages installed."

# ── 2. Python packages ────────────────────────────────────────────────────────
section "Installing Python packages"
pip3 install python-dotenv --break-system-packages
info "Python packages installed."

# ── 3. .env configuration ─────────────────────────────────────────────────────
section "Configuring credentials"

if [ -f "${ENV_FILE}" ]; then
    warn ".env already exists — skipping credential setup."
    warn "Edit ${ENV_FILE} manually if you need to change anything."
else
    cp "${REPO_DIR}/.env.example" "${ENV_FILE}"

    echo ""
    echo "  Enter your SIP credentials (leave blank to fill in later):"
    echo ""

    read -p "  SIP server hostname  [e.g. pbx.example.com] : " sip_server
    read -p "  SIP username / ext   [e.g. 1001]            : " sip_user
    read -s -p "  SIP password                                : " sip_password
    echo ""
    echo ""
    info "Available audio devices:"
    aplay -l 2>/dev/null | grep "^card" || echo "  (none found — check USB audio dongle)"
    echo ""
    read -p "  AUDIO_DEVICE  [e.g. alsa,plughw:1,0] : " audio_device
    audio_device="${audio_device:-default}"

    # Write .env with quoted string values to match expected format
    printf 'SIP_SERVER="%s"\nSIP_USER="%s"\nSIP_PASSWORD="%s"\nAUDIO_DEVICE=%s\n' \
        "$sip_server" "$sip_user" "$sip_password" "$audio_device" > "${ENV_FILE}"

    chmod 600 "${ENV_FILE}"
    info ".env written."
fi

# ── 4. systemd service ────────────────────────────────────────────────────────
section "Setting up systemd service"

sudo tee "${SERVICE_FILE}" > /dev/null <<EOF
[Unit]
Description=Rotary Phone SIP Bridge
After=network.target sound.target

[Service]
ExecStart=${PYTHON} ${SCRIPT}
WorkingDirectory=${REPO_DIR}
Restart=on-failure
RestartSec=5
User=${CURRENT_USER}
EnvironmentFile=${ENV_FILE}

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"
sudo systemctl restart "${SERVICE_NAME}"
info "Service '${SERVICE_NAME}' enabled and started."

# ── 5. Auto-update script ─────────────────────────────────────────────────────
section "Creating auto-update script"

cat > "${UPDATE_SCRIPT}" <<'UPDATEEOF'
#!/usr/bin/env bash
# Auto-update script — run by cron every 5 minutes
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="rotary-phone"
LOG="${REPO_DIR}/update.log"

cd "${REPO_DIR}" || exit 1

BEFORE="$(git rev-parse HEAD 2>/dev/null)"
git fetch origin main --quiet >> "${LOG}" 2>&1
REMOTE="$(git rev-parse origin/main 2>/dev/null)"

if [ "${BEFORE}" = "${REMOTE}" ]; then
    exit 0   # nothing to do
fi

echo "$(date '+%Y-%m-%d %H:%M:%S') — update available: ${BEFORE:0:7} → ${REMOTE:0:7}" >> "${LOG}"

# Check if a call is active via baresip ctrl socket
# Sends a callstat command and looks for an active call response
CALL_ACTIVE=0
if command -v nc &>/dev/null; then
    STAT=$(echo '3:{"command":"callstat"},,' | nc -q1 -w2 127.0.0.1 4444 2>/dev/null || true)
    if echo "${STAT}" | grep -qi "active\|established\|in_call"; then
        CALL_ACTIVE=1
    fi
fi

if [ "${CALL_ACTIVE}" -eq 1 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') — call in progress, skipping restart." >> "${LOG}"
    exit 0
fi

# Pull and restart
git pull origin main --quiet >> "${LOG}" 2>&1
sudo systemctl restart "${SERVICE_NAME}" >> "${LOG}" 2>&1
echo "$(date '+%Y-%m-%d %H:%M:%S') — restarted ${SERVICE_NAME} after update." >> "${LOG}"
UPDATEEOF

chmod +x "${UPDATE_SCRIPT}"
info "update.sh created at ${UPDATE_SCRIPT}"

# ── 6. sudo rule for update script (so it can restart service without password) ─
section "Configuring passwordless restart for update script"

SUDOERS_LINE="${CURRENT_USER} ALL=(ALL) NOPASSWD: /bin/systemctl restart ${SERVICE_NAME}"
SUDOERS_FILE="/etc/sudoers.d/rotary-phone-update"

echo "${SUDOERS_LINE}" | sudo tee "${SUDOERS_FILE}" > /dev/null
sudo chmod 440 "${SUDOERS_FILE}"
info "Sudoers rule added: ${SUDOERS_FILE}"

# ── 7. Cron job ───────────────────────────────────────────────────────────────
section "Installing cron job (every 5 minutes)"

CRON_LINE="*/5 * * * * ${UPDATE_SCRIPT} >> ${LOG} 2>&1"

# Add only if not already present
( crontab -l 2>/dev/null | grep -v "${UPDATE_SCRIPT}" ; echo "${CRON_LINE}" ) | crontab -
info "Cron job installed."

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "  ╔═══════════════════════════════════════╗"
echo "  ║          Setup complete ✓             ║"
echo "  ╚═══════════════════════════════════════╝"
echo ""
echo "  Service status : sudo systemctl status ${SERVICE_NAME}"
echo "  Live logs      : sudo journalctl -u ${SERVICE_NAME} -f"
echo "  Update log     : tail -f ${LOG}"
echo ""