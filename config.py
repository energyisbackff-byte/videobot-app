"""Configuration — everything is read from environment variables.
Copy .env.example to .env and fill it in, or export the variables."""
import os

def _load_dotenv(path=".env"):
    """Tiny .env loader so we don't need python-dotenv (a-Shell friendly)."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

_load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")          # from @BotFather
FAL_KEY   = os.environ.get("FAL_KEY", "")            # from fal.ai dashboard
ADMIN_ID  = int(os.environ.get("ADMIN_ID", "0"))     # your Telegram user id (free gens)

# ---- Pricing tiers (amounts are in Telegram Stars) ----
# cost_usd is an estimate from fal.ai per-second rates (6s clip) — used only
# for your own margin info in /plans, the user pays `stars`.
TIERS = {
    "standard": {
        "label": "Standard (Kling Turbo)",
        "stars": 40,
        "cost_usd": 0.42,          # ~$0.07/s x 6s
        "model": "fal-ai/kling-video/v2.5-turbo/pro/text-to-video",
        "model_i2v": "fal-ai/kling-video/v2.5-turbo/pro/image-to-video",
    },
    "pro": {
        "label": "Pro (Kling 3.0)",
        "stars": 100,
        "cost_usd": 1.00,          # ~$0.168/s x 6s
        # VERIFY the exact v3 model id on https://fal.ai/models before launch
        "model": "fal-ai/kling-video/v3/pro/text-to-video",
        "model_i2v": "fal-ai/kling-video/v3/pro/image-to-video",
    },
    "ultra": {
        "label": "Ultra (Veo 3.1)",
        "stars": 200,
        "cost_usd": 2.40,          # ~$0.40/s x 6s
        "model": "fal-ai/veo3",
        "model_i2v": "fal-ai/veo3",
    },
}

CLIP_SECONDS   = "5"          # fal duration string ("5" or "10" for kling)
ASPECT_RATIO   = "9:16"       # vertical by default
MAX_JOBS_PER_DAY_PER_USER = 10

# --- Subscription / crypto payment settings (all editable live via /set) ---
WALLET_ADDRESS = os.environ.get("WALLET_ADDRESS", "PUT_YOUR_USDT_TRC20_ADDRESS_HERE")
MONTHLY_PRICE_USD = float(os.environ.get("MONTHLY_PRICE_USD", "20"))
SUB_DAYS = int(os.environ.get("SUB_DAYS", "30"))
MONTHLY_VIDEO_LIMIT = int(os.environ.get("MONTHLY_VIDEO_LIMIT", "30"))
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "savedby100x")
FREE_MODE = os.environ.get("FREE_MODE", "0") == "1"   # /set FREE_MODE=1 = all free

# Light-touch content guard: block requests that target real private people
# or ask for deceptive "real footage" framing. Extend as you like.
BLOCKED_TERMS = [
    "real name", "my ex", "girlfriend's face", "boyfriend's face",
    "celebrity face", "deepfake of", "fake news footage", "cctv of a real",
]
