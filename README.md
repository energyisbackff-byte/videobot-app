# AI Video Studio Bot

Telegram bot that sells AI-generated videos. Users describe a video
(optionally attach a reference photo), subscribe monthly in USDT-TRC20
(auto-detected on-chain), and receive generated clips in chat.

Public read-only mirror used by the server's `/update` command.
Secrets (.env) are git-ignored and NEVER committed.

## Architecture

- `bot.py` — Telegram bot: commands, subscription gate, job queue, delivery,
  admin panel (server control + subscriber management + /update)
- `fal_provider.py` — fal.ai queue API: submit → poll → download, image upload
- `subs.py` — subscription storage + USDT-TRC20 on-chain payment watcher
- `config.py` — tokens, tiers, prices, limits, content guard

## Admin commands (admin Telegram ID only)

/status · /logs [n] · /restart · /reboot YES · /set KEY=VALUE · /exec <cmd>
/update (git pull + restart) · /approve <id> [days] · /revoke <id> · /subs

## Payments

Monthly subscription in USDT-TRC20. Each user gets a unique amount
(price + unique cents); the on-chain watcher auto-approves when the exact
amount lands. See GIT_SETUP.md for the /update workflow.
