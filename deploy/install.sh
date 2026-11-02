#!/usr/bin/env bash
#
# Idempotent deployment script for DroidHub.
#
# Usage:
#   sudo ./deploy/install.sh            # Run installation
#   ./deploy/install.sh --dry-run       # Preview actions without making changes
#
set -euo pipefail

DRY_RUN=false
for arg in "$@"; do
    case "$arg" in
        --dry-run|-n)
            DRY_RUN=true
            ;;
        --help|-h)
            echo "Usage: $0 [--dry-run]"
            echo "Options:"
            echo "  --dry-run, -n    Preview actions without making changes"
            echo "  --help, -h       Show this help message"
            exit 0
            ;;
        *)
            echo "Unknown option: $arg"
            echo "Use --help for usage information."
            exit 1
            ;;
    esac
done

# Text formatting helpers
log() {
    echo -e "\033[1;34m[INFO]\033[0m $*"
}

warn() {
    echo -e "\033[1;33m[WARN]\033[0m $*"
}

err() {
    echo -e "\033[1;31m[ERROR]\033[0m $*" >&2
}

run_cmd() {
    if [ "$DRY_RUN" = true ]; then
        echo -e "\033[1;32m[DRY-RUN]\033[0m $*"
    else
        "$@"
    fi
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TARGET_DIR="/opt/droidhub"
SERVICE_NAME="droidhub.service"
CADDYFILE="/etc/caddy/Caddyfile"
HUB_DOMAIN="hub.193-41-86-92.sslip.io"

echo "=========================================="
echo "DroidHub Deployment"
if [ "$DRY_RUN" = true ]; then
    echo "Mode: DRY RUN (no modifications will be made)"
else
    echo "Mode: LIVE INSTALLATION"
fi
echo "=========================================="

# Privilege check
if [ "$DRY_RUN" = false ] && [ "$(id -u)" -ne 0 ]; then
    err "This script must be run as root (or with sudo)."
    err "To preview changes without root privileges, run: $0 --dry-run"
    exit 1
fi

# Pre-flight checks: verify required and recommended system tools before making changes
log "Verifying system prerequisites..."
if command -v python3 >/dev/null 2>&1; then
    log "Python 3 is installed: $(python3 --version 2>&1)"
else
    err "Python 3 is not installed. Please install Python 3.11+ before proceeding."
    exit 1
fi

if ! python3 -c "import venv" >/dev/null 2>&1; then
    warn "python3-venv is not installed or incomplete. You may need to run: sudo apt-get install -y python3-venv"
else
    log "python3-venv is available."
fi

if command -v adb >/dev/null 2>&1; then
    log "ADB is installed: $(adb --version | head -n 1)"
else
    warn "ADB is not installed on the system. Install it via: sudo apt-get update && sudo apt-get install -y adb"
fi

if command -v tailscale >/dev/null 2>&1; then
    log "Tailscale is installed."
else
    warn "Tailscale is not installed on the system. Install it via: curl -fsSL https://tailscale.com/install.sh | sh"
fi

# 1. System user 'hub'
log "1. Checking system user 'hub'..."
if id -u hub >/dev/null 2>&1; then
    log "User 'hub' already exists. Skipping user creation."
else
    log "Creating system user and group 'hub'..."
    run_cmd useradd --system --user-group --shell /usr/sbin/nologin --home-dir "$TARGET_DIR" hub
fi

# 2. Directory structure and file sync
log "2. Preparing directory structure at $TARGET_DIR..."
run_cmd mkdir -p "$TARGET_DIR" "$TARGET_DIR/data" "$TARGET_DIR/config" "$TARGET_DIR/.android"

log "Syncing application code and configuration files..."
if [ "$REPO_DIR" != "$TARGET_DIR" ]; then
    run_cmd cp -r "$REPO_DIR/hub" "$TARGET_DIR/"
    run_cmd cp "$REPO_DIR/requirements.txt" "$TARGET_DIR/"
    if [ -f "$REPO_DIR/pyproject.toml" ]; then
        run_cmd cp "$REPO_DIR/pyproject.toml" "$TARGET_DIR/"
    fi

    # config/devices.yaml: copy if not already present
    if [ ! -f "$TARGET_DIR/config/devices.yaml" ]; then
        log "Installing initial config/devices.yaml..."
        run_cmd cp "$REPO_DIR/config/devices.yaml" "$TARGET_DIR/config/devices.yaml"
    else
        log "$TARGET_DIR/config/devices.yaml exists. Preserving existing device configurations."
    fi

    # .env: NEVER overwrite existing .env
    if [ ! -f "$TARGET_DIR/.env" ]; then
        log "No .env found in $TARGET_DIR. Creating from .env.example..."
        run_cmd cp "$REPO_DIR/.env.example" "$TARGET_DIR/.env"
        run_cmd chmod 600 "$TARGET_DIR/.env"
        warn "Created initial $TARGET_DIR/.env."
        warn "IMPORTANT: Edit $TARGET_DIR/.env and set HUB_ADMIN_PASSWORD_HASH, HUB_SESSION_SECRET, HUB_MCP_TOKEN, HUB_API_TOKEN before starting!"
    else
        log "$TARGET_DIR/.env already exists. Preserving existing environment secrets."
    fi
else
    log "Repository directory is target directory ($TARGET_DIR). Skipping file copy."
    if [ ! -f "$TARGET_DIR/.env" ] && [ -f "$TARGET_DIR/.env.example" ]; then
        log "No .env found in $TARGET_DIR. Creating from .env.example..."
        run_cmd cp "$TARGET_DIR/.env.example" "$TARGET_DIR/.env"
        run_cmd chmod 600 "$TARGET_DIR/.env"
        warn "Created initial $TARGET_DIR/.env."
        warn "IMPORTANT: Edit $TARGET_DIR/.env and set HUB_ADMIN_PASSWORD_HASH, HUB_SESSION_SECRET, HUB_MCP_TOKEN, HUB_API_TOKEN before starting!"
    fi
fi

# 3. Python virtual environment and dependencies
log "3. Setting up Python virtual environment..."
VENV_DIR="$TARGET_DIR/venv"
if [ ! -d "$VENV_DIR" ]; then
    log "Creating virtual environment in $VENV_DIR..."
    run_cmd python3 -m venv "$VENV_DIR"
else
    log "Virtual environment already exists in $VENV_DIR."
fi

log "Installing/updating dependencies from requirements.txt..."
run_cmd "$VENV_DIR/bin/pip" install --quiet --upgrade pip
run_cmd "$VENV_DIR/bin/pip" install --quiet -r "$TARGET_DIR/requirements.txt"

# Set directory ownership and permissions (performed after venv creation so venv is owned by hub)
log "Configuring ownership and permissions for user 'hub'..."
run_cmd chown -R hub:hub "$TARGET_DIR"
run_cmd chmod 750 "$TARGET_DIR"
run_cmd chmod 770 "$TARGET_DIR/data" "$TARGET_DIR/config"
run_cmd chmod 700 "$TARGET_DIR/.android"

# 4. Systemd service installation
log "4. Installing systemd service..."
SYSTEMD_DEST="/etc/systemd/system/$SERVICE_NAME"
run_cmd cp "$SCRIPT_DIR/droidhub.service" "$SYSTEMD_DEST"
run_cmd chmod 644 "$SYSTEMD_DEST"

if command -v systemctl >/dev/null 2>&1; then
    run_cmd systemctl daemon-reload
    run_cmd systemctl enable "$SERVICE_NAME"
    if [ "$DRY_RUN" = false ]; then
        log "Service $SERVICE_NAME enabled."
    fi
else
    warn "systemctl command not found. Service unit copied to $SYSTEMD_DEST."
fi

# 5. Caddy reverse proxy configuration
log "5. Checking Caddy configuration..."
if [ -f "$CADDYFILE" ]; then
    if grep -q "$HUB_DOMAIN" "$CADDYFILE"; then
        log "Domain $HUB_DOMAIN is already present in $CADDYFILE. Leaving Caddyfile unchanged."
    else
        log "Adding snippet for $HUB_DOMAIN to $CADDYFILE (existing gemini-pool block preserved)..."
        if [ "$DRY_RUN" = true ]; then
            echo -e "\033[1;32m[DRY-RUN]\033[0m Backup $CADDYFILE to ${CADDYFILE}.bak.<timestamp>"
            echo -e "\033[1;32m[DRY-RUN]\033[0m Append $SCRIPT_DIR/Caddyfile.snippet to $CADDYFILE"
            echo -e "\033[1;32m[DRY-RUN]\033[0m caddy validate --config $CADDYFILE && systemctl reload caddy"
        else
            BACKUP_FILE="${CADDYFILE}.bak.$(date +%Y%m%d%H%M%S)"
            cp "$CADDYFILE" "$BACKUP_FILE"
            log "Created Caddyfile backup at $BACKUP_FILE."
            echo "" >> "$CADDYFILE"
            cat "$SCRIPT_DIR/Caddyfile.snippet" >> "$CADDYFILE"
            if command -v caddy >/dev/null 2>&1; then
                if caddy validate --config "$CADDYFILE" >/dev/null 2>&1; then
                    log "Caddyfile configuration validated successfully."
                    if command -v systemctl >/dev/null 2>&1; then
                        systemctl reload caddy
                        log "Caddy reloaded successfully."
                    fi
                else
                    warn "Caddyfile validation failed! Restoring backup..."
                    cp "$BACKUP_FILE" "$CADDYFILE"
                fi
            fi
        fi
    fi
else
    warn "Caddyfile not found at $CADDYFILE."
    warn "When Caddy is set up, append $SCRIPT_DIR/Caddyfile.snippet to your Caddyfile."
fi

echo "=========================================="
log "Installation procedure completed."
if [ "$DRY_RUN" = false ]; then
    echo ""
    echo "Next steps:"
    echo "1. Verify configuration: edit $TARGET_DIR/.env if needed."
    echo "2. Add your phone IPs in $TARGET_DIR/config/devices.yaml."
    echo "3. Start the service: sudo systemctl start $SERVICE_NAME"
    echo "4. Check service status: sudo systemctl status $SERVICE_NAME"
    echo "5. Check logs: sudo journalctl -u $SERVICE_NAME -f"
    echo "=========================================="
fi
