#!/usr/bin/env bash
# deploy.sh — Hot-swap the running battery manager on the server.
# Usage: bash deploy.sh [ssh_host]
# Example: bash deploy.sh ananth@192.168.1.14
#
# What it does:
#   1. Copies battery_manager.py to the server
#   2. Kills the old instance gracefully (waits for PID file to clear)
#   3. Starts the new instance in the background

set -e
HOST=""
REMOTE_DIR="~/battery_manager"
SCRIPT="battery_manager.py"

echo "==> Deploying to System.Management.Automation.Internal.Host.InternalHost:/"

# 1. Ensure remote directory exists
ssh "System.Management.Automation.Internal.Host.InternalHost" "mkdir -p "

# 2. Copy script
scp "" "System.Management.Automation.Internal.Host.InternalHost:/"
echo "==> Copied "

# 3. Kill old instance (ignore error if not running)
ssh "System.Management.Automation.Internal.Host.InternalHost" "pkill -f  || true; sleep 3; rm -f /data/battery_manager.pid"
echo "==> Killed old instance"

# 4. Start new instance
ssh "System.Management.Automation.Internal.Host.InternalHost" "cd  && nohup ~/venv/bin/python3  >> /data/battery_manager.log 2>&1 &"
echo "==> Started new instance"

# 5. Verify
sleep 5
ssh "System.Management.Automation.Internal.Host.InternalHost" "ps aux | grep  | grep -v grep && tail -5 /data/battery_manager.log"
echo "==> Deploy complete"
