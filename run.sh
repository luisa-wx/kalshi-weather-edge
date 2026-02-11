#!/bin/bash
# WX Sniper V2 - NWWS-OI Ingestor Runner
# 
# Usage:
#   ./run.sh                    # Normal mode (only temp-related products)
#   ./run.sh --watch-all        # Log ALL products (noisy but useful for debugging)
#   ./run.sh --debug            # Full debug logging including XMPP protocol
#
# Before first run:
#   1. cp .env.template .env
#   2. Fill in your NWWS-OI credentials in .env
#   3. pip install -r requirements.txt

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Load .env file
if [ -f .env ]; then
    export $(grep -v '^#' .env | grep -v '^\s*$' | xargs)
else
    echo "ERROR: .env file not found. Copy .env.template to .env and fill in credentials."
    exit 1
fi

# Validate required vars
if [ -z "$NWWS_USER_ID" ] || [ "$NWWS_USER_ID" = "your_user_id_here" ]; then
    echo "ERROR: NWWS_USER_ID not set in .env"
    exit 1
fi

if [ -z "$NWWS_PASSWORD" ] || [ "$NWWS_PASSWORD" = "your_password_here" ]; then
    echo "ERROR: NWWS_PASSWORD not set in .env"
    exit 1
fi

SERVER="${NWWS_SERVER:-nwws-oi-bldr.weather.gov}"
JID="${NWWS_USER_ID}@${SERVER}"

echo "Connecting as: ${JID}"
echo "Server: ${SERVER}"

python3 nwws_client.py \
    --jid "$JID" \
    --password "$NWWS_PASSWORD" \
    "$@"
