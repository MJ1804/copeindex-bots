# CopeIndex Telegram Bots
#
# Shared config between all three bots.
# Environment variables inject via Railway (or local .env).

import os

# --- Database ---
DATABASE_POOLED_URL = os.environ["DATABASE_POOLED_URL"]
DATABASE_URL = os.environ["DATABASE_URL"]  # direct connect for migrations

# --- Telegram Tokens ---
CROWN_BOT_TOKEN     = os.environ["CROWN_BOT_TOKEN"]
FEED_BOT_TOKEN      = os.environ["FEED_BOT_TOKEN"]
GRIND_BOT_TOKEN     = os.environ["GRIND_BOT_TOKEN"]

# --- Channel IDs (where the bots post) ---
CROWN_CHANNEL_ID    = os.environ.get("CROWN_CHANNEL_ID", "-5292453375")
FEED_CHANNEL_ID     = os.environ.get("FEED_CHANNEL_ID", "-5292453375")
GRIND_CHANNEL_ID    = os.environ.get("GRIND_CHANNEL_ID", "-5292453375")

# --- CopeIndex constants ---
COPE_TOKEN_ADDRESS  = os.environ.get("COPE_TOKEN_ADDRESS", "0x...placeholder")
UNISWAP_POOL        = os.environ.get("UNISWAP_POOL", "0x...placeholder")
WETH_ADDRESS        = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
