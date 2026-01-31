#!/bin/bash
# WX Sniper EC2 Deployment Script
# Run this on a fresh Ubuntu 24.04 EC2 instance in us-east-1
#
# Usage: 
#   wget https://raw.githubusercontent.com/your-user/wx-sniper/main/deploy-ec2.sh
#   chmod +x deploy-ec2.sh
#   ./deploy-ec2.sh

set -e

echo "================================================"
echo "WX Sniper EC2 Deployment Script"
echo "================================================"

# Update system
echo "[1/7] Updating system..."
sudo apt update && sudo apt upgrade -y

# Install Python 3.11
echo "[2/7] Installing Python 3.11..."
sudo apt install -y python3.11 python3.11-venv python3-pip git curl

# Clone repository
echo "[3/7] Cloning repository..."
cd /home/ubuntu
if [ -d "wx-sniper" ]; then
    echo "  Repository exists, pulling latest..."
    cd wx-sniper && git pull
else
    # Replace with your actual repo URL
    git clone https://github.com/YOUR-USERNAME/wx-sniper.git
    cd wx-sniper
fi

# Create virtual environment
echo "[4/7] Creating virtual environment..."
python3.11 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# Create .env file if it doesn't exist
echo "[5/7] Setting up environment..."
if [ ! -f ".env" ]; then
    echo "  Creating .env from template..."
    cp .env.template .env
    echo ""
    echo "  ⚠️  IMPORTANT: Edit .env with your Kalshi credentials!"
    echo "     nano /home/ubuntu/wx-sniper/.env"
    echo ""
fi

# Install systemd service
echo "[6/7] Installing systemd service..."
sudo cp wx-sniper.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable wx-sniper

# Test run
echo "[7/7] Testing (dry run)..."
timeout 30 python smart_poller.py || true

echo ""
echo "================================================"
echo "✅ Deployment complete!"
echo "================================================"
echo ""
echo "Next steps:"
echo "  1. Edit credentials:  nano /home/ubuntu/wx-sniper/.env"
echo "  2. Start service:     sudo systemctl start wx-sniper"
echo "  3. Check status:      sudo systemctl status wx-sniper"
echo "  4. View logs:         journalctl -u wx-sniper -f"
echo "  5. Dashboard:         http://<your-ec2-ip>:8080"
echo ""
echo "To enable LIVE trading, set LIVE_MODE=true in .env and restart:"
echo "  sudo systemctl restart wx-sniper"
echo ""
