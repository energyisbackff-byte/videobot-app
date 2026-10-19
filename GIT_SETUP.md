# Git /update setup (public mirror, no tokens)

The server syncs code from the PUBLIC repo `videobot-app`. Public repos pull
WITHOUT any token — nothing to create, nothing to rotate. Only the owner's
GitHub account can push, so nobody else can edit the code. `.env` with your
keys is git-ignored and NEVER leaves the server.

## Server wiring (one /exec in Telegram)

```
/exec true; apt install -y git && cd /root/videobot && git init && git remote add origin https://github.com/energyisbackff-byte/videobot-app.git && git fetch origin && git checkout -f -t origin/main && systemctl restart videobot && echo GIT_SYNCED
```

## Daily life

- `/update` — server pulls newest code and restarts
- Code changes get pushed to the repo for you (via GitHub MCP) — you only
  ever run `/update`

## Rules

- NEVER commit `.env` (the .gitignore prevents it — do not remove it)
- Code is publicly readable by design; the keys are what matter and they
  never touch GitHub. To go private later: flip visibility in Settings and
  add a read-only token to the remote URL.
