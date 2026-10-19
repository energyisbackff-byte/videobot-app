# Deploy the bot on Vultr — copy-paste guide (~15 min)

The bot just relays API calls, so the CHEAPEST server is enough.
No GPU needed — video generation happens on fal.ai's servers.

## 1. Create the server (Vultr dashboard, 3 min)

1. Vultr dashboard → **Deploy New Server**
2. **Cloud Compute – Shared CPU**
3. Location: closest to you
4. Image: **Ubuntu 24.04 LTS x64**
5. Plan: the cheapest regular one (~$5/mo, 1 vCPU / 1 GB)
6. Deploy → wait until it shows **Running**, then copy the **IP address**
   and the **root password** (eye icon).

## 2. Upload the bot

```bash
scp telegram_video_bot.zip root@SERVER_IP:~/
```

## 3. Set up the server (SSH into it)

```bash
ssh root@SERVER_IP
apt update && apt install -y python3-pip unzip
unzip telegram_video_bot.zip && mv telegram_video_bot videobot
cd videobot
pip3 install --break-system-packages -r requirements.txt
```

## 4. Add your keys to .env, then:

```bash
python3 bot.py            # test — should print "Bot starting (polling)…"
cp videobot.service /etc/systemd/system/
systemctl enable --now videobot
systemctl status videobot
```

Logs anytime: `journalctl -u videobot -f`
