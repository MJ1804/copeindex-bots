# CopeIndex Bots 🤖

Three Telegram bots powering the CopeIndex ecosystem:

| Bot | Token Env Var | What It Does |
|---|---|---|
| 👑 Crown Announcer | `CROWN_BOT_TOKEN` | Crown winner daily at 00:05 UTC + COPE burn |
| 📈 Buy-Feed | `FEED_BOT_TOKEN` | COPE/ETH swap notifications live |
| 🎮 Grind Leaderboard | `GRIND_BOT_TOKEN` | Chat participation points + /rank + weekly post |

## Architecture

```
bots/
├── crown/bot.py    # APScheduler → 00:05 UTC job → Telegram post
├── feed/bot.py     # Poll loop → Uniswap events → Telegram post
├── grind/bot.py    # python-telegram-bot → message handler → PostgreSQL
shared/
├── config.py       # All env vars → Python constants
├── models.py       # SQLAlchemy ORM (crown_winners, swap_events, chat_messages)
└── migrate.py      # CREATE TABLE IF NOT EXISTS
```

### Stack

- **Python 3.11+** with `python-telegram-bot`, `APScheduler`, `SQLAlchemy`
- **Neon PostgreSQL** (serverless Postgres)
- **Railway** (hosting — free tier enough for 3 bots)

## Deploying to Railway

### 1. Add environment variables in Railway Dashboard

```
DATABASE_POOLED_URL=postgresql://user:pass@ep-xxx-pooler...neon.tech/neondb?sslmode=require
DATABASE_URL=postgresql://user:pass@ep-xxx...neon.tech/neondb?sslmode=require

CROWN_BOT_TOKEN=8943753710:...
FEED_BOT_TOKEN=8874907088:...
GRIND_BOT_TOKEN=8187955875:...

CROWN_CHANNEL_ID=@YourChannel     # Telegram channel username or chat id
FEED_CHANNEL_ID=@YourChannel
GRIND_CHANNEL_ID=@YourGroup       # Grind bot listens to a group, posts here
```

### 2. Create 3 Railway services from the same repo

In Railway dashboard:

- **New Service** → GitHub repo → add `CROWN_BOT_TOKEN` + `CROWN_CHANNEL_ID` as env vars
- Start command: `python bots/crown/bot.py`

Repeat for `feed` and `grind`.

### 3. Run migrations

```bash
# One-time — from Railway CLI or local:
python shared/migrate.py
```

Or let `start.sh` do it on each deploy (idempotent).

## Local Development

```bash
# Copy secrets file, fill in tokens
cp .env.example .env
source .env

pip install -r requirements.txt
python shared/migrate.py

# Run any bot locally:
python bots/crown/bot.py
```

## Channel Setup (before launch)

1. Create Telegram channel/group for each bot
2. Add each bot as admin to its channel
3. Set `*_CHANNEL_ID` env vars to the channel usernames or numeric IDs
4. For Grind: bot must be in the group to receive messages

## Phase 7 — Pre-Launch Checklist

- [ ] Verify Fusi.ng V2 contract addresses (update `COPE_TOKEN_ADDRESS`, `UNISWAP_POOL` in shared/config.py)
- [ ] Fill all production placeholders
- [ ] Final security review
- [ ] Owner sign-off

## Phase 8 — Mainnet Launch Sequence

1. Deploy COPE token via Fusi.ng V2
2. Create COPE/ETH Uniswap V4 pool
3. Register 5 payees for fee split
4. Update `shared/config.py` with real contract addresses
5. Redeploy all 3 bots
6. First manual Crown burn