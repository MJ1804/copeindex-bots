"""
Buy-Feed Bot — watches COPE/ETH swaps and posts notifications.

Filters:
  - Only posts BUY swaps ≥ $50 USD
  - Sends branded notification image with every post
  - Deduplicates via swap_events.tx_hash unique constraint
  - Stores swap history in Neon DB

Swap detection:
  - Alchemy Transfers API polling every 15s (no trace API needed)
  - Scans Transfer events on COPE ERC-20 contract
  - Transfer TO pool = BUY, Transfer FROM pool = SELL
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

from sqlalchemy import select
from telegram import Bot, InputMediaPhoto
from telegram.error import TelegramError

from shared.config import (
    FEED_BOT_TOKEN,
    FEED_CHANNEL_ID,
)
from shared.models import SwapEvent, get_engine, get_session

from bots.feed.swap_detector import fetch_recent_swaps as _fetch_swaps

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [feed] %(levelname)s %(message)s",
)
log = logging.getLogger("feed")

POLL_INTERVAL   = int(os.environ.get("FEED_POLL_SECONDS", "15"))
BUY_MIN_USD     = float(os.environ.get("BUY_MIN_USD", "10"))
COPE_BUY_IMAGE  = os.environ.get(
    "COPE_BUY_IMAGE",
    "https://raw.githubusercontent.com/MJ1804/copeindex-bots/main/bots/feed/cope_buy.png",
)


# ── Swap detection (placeholder) ────────────────────────

async def fetch_recent_swaps(session) -> list[dict]:
    """
    Poll Alchemy for recent COPE Transfer events involving the Uniswap pool.
    Delegated to swap_detector.py.
    """
    return await _fetch_swaps(session)


# ── Formatting ──────────────────────────────────────────

def should_post(swap: dict) -> bool:
    """Only post buys ≥ BUY_MIN_USD threshold."""
    eth_val  = swap.get("amount_eth", 0)
    price_usd = swap.get("price_usd") or 0
    amount_cope = swap.get("amount_cope", 0)
    if eth_val > 0 and price_usd > 0:
        usd = eth_val * price_usd
    else:
        usd = price_usd * amount_cope
    passes = swap.get("side") == "BUY" and usd >= BUY_MIN_USD
    if not passes and amount_cope > 0:
        log.debug("Skipping %s — side=%s usd=%.2f (need %.2f) cope=%.0f eth=%.4f",
                  swap.get("tx_hash","?")[:10], swap.get("side"),
                  usd, BUY_MIN_USD, amount_cope, eth_val)
    return passes


def format_swap(swap: dict) -> str:
    """Build the swap notification caption."""
    usd = (swap.get("price_usd") or 0) * swap.get("amount_cope", 0)
    return (
        f"🟢 <b>BUY — COPE</b>\n\n"
        f"💰 <b>{swap['amount_cope']:,.0f} COPE</b>\n"
        f"⚡ {swap['amount_eth']:,.4f} ETH\n"
        f"💵 <b>${usd:,.2f} USD</b>\n"
        f"🔗 <a href=\"https://etherscan.io/tx/{swap['tx_hash']}\">View on Etherscan ↗</a>"
    )


# ── Main loop ───────────────────────────────────────────

async def process_swaps():
    engine = get_engine()
    session = get_session(engine)
    bot = Bot(token=FEED_BOT_TOKEN)

    try:
        swaps = await fetch_recent_swaps(session)

        for sw in swaps:
            # Deduplicate
            exists = session.scalar(
                select(SwapEvent).where(SwapEvent.tx_hash == sw["tx_hash"])
            )
            if exists:
                continue

            # Apply $50 buy filter
            if not should_post(sw):
                log.debug("Skipping %s — below $50 threshold", sw["tx_hash"][:10])
                continue

            record = SwapEvent(
                tx_hash=sw["tx_hash"],
                block_number=sw["block_number"],
                timestamp=sw["timestamp"],
                pair=sw.get("pair", "COPE/ETH"),
                side=sw["side"],
                amount_cope=sw["amount_cope"],
                amount_eth=sw["amount_eth"],
                price_usd=sw.get("price_usd"),
                maker=sw.get("maker"),
                notified=False,
            )
            session.add(record)

            # Send with image
            try:
                await bot.send_media_group(
                    chat_id=FEED_CHANNEL_ID,
                    media=[
                        InputMediaPhoto(
                            url=COPE_BUY_IMAGE,
                            caption=format_swap(sw),
                            parse_mode="HTML",
                        )
                    ],
                )
                record.notified = True
                record.notified_at = datetime.now(timezone.utc)
                log.info("📊 Posted BUY %,.0f COPE (~$%.2f) — %s",
                         sw["amount_cope"],
                         (sw.get("price_usd") or 0) * sw["amount_cope"],
                         sw["tx_hash"][:10])
            except TelegramError as e:
                log.error("Telegram send failed for tx %s: %s", sw["tx_hash"], e)

        session.commit()
    except Exception:
        log.exception("process_swaps crashed")
        session.rollback()
    finally:
        session.close()
        engine.dispose()


# ── Entry ───────────────────────────────────────────────

async def main():
    log.info("📈 Buy-Feed Bot starting — posting buys ≥ $%.0f, poll every %ds",
             BUY_MIN_USD, POLL_INTERVAL)

    engine = get_engine()
    from shared.models import create_all
    create_all(engine)
    engine.dispose()

    try:
        while True:
            await process_swaps()
            await asyncio.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        log.info("Shutting down")


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(main())
    loop.close()
