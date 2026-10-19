#!/bin/bash
# AI Video Bot — one-shot Vultr setup. Run: bash setup_server.sh
set -e
cd ~

echo ">>> Installing system packages..."
apt update -qq && apt install -y -qq python3-pip unzip

echo ">>> Unpacking bot..."
unzip -o -q telegram_video_bot.zip
rm -rf videobot
mv telegram_video_bot videobot
cd videobot

echo ">>> Installing Python packages (1-2 min)..."
pip3 install --break-system-packages -q -r requirements.txt

echo ">>> Installing 24/7 service..."
cp videobot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now videobot

sleep 4
echo ""
echo "================ RESULT ================"
systemctl status videobot --no-pager | head -12
echo ""
echo ">>> Last log lines:"
journalctl -u videobot -n 10 --no-pager
echo ""
echo "If you see 'Bot starting (polling)' above — DONE."
echo "Open @CHANGE_SIBOT in Telegram and send /generate"
