"""
Buy-Feed Bot — watches COPE/ETH swaps and posts notifications.

Architecture:
  - Polls Uniswap pool events every N seconds (configurable)
  - Deduplicates via swap_events.tx_hash unique constraint
  - Posts formatted buy/sell notifications to Telegram channel
  - Stores swap history in Neon DB

Phase 8 integration:
  - Replace polling with live WebSocket subscription to Uniswap V4 events
  - Real COPE token address + pool address
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

from sqlalchemy import select
from telegram import Bot
from telegram.error import TelegramError

from shared.config import FEED_BOT_TOKEN, FEED_CHANNEL_ID, COPE_TOKEN_ADDRESS, UNISWAP_POOL
from shared.models import SwapEvent, get_engine, get_session

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [feed] %(levelname)s %(message)s",
)
log = logging.getLogger("feed")

POLL_INTERVAL = int(os.environ.get("FEED_POLL_SECONDS", "15"))


# ── Swap detection (placeholder) ────────────────────────

async def fetch_recent_swaps() -> list[dict]:
    """
    Query Uniswap V4 pool events for recent COPE/ETH swaps.

    Placeholder — returns empty list. Phase 8 replaces this with either:
      a) web3.py EventLog query on the Uniswap V4 pool contract
      b) WebSocket subscription via Alchemy / Infura / your own RPC

    Returns list of dicts:
        {tx_hash, block_number, timestamp, side, amount_cope, amount_eth, maker}
    """
    # TODO: Real Uniswap V4 swap event query
    return []


# ── Formatting ──────────────────────────────────────────

def format_swap(swap: dict) -> str:
    """Build one swap notification line."""
    side_emoji = "🟢" if swap["side"] == "BUY" else "🔴"
    amount = f"{swap['amount_cope']:,.0f} COPE"
    eth = f"{swap['amount_eth']:,.4f} ETH"

    return (
        f"{side_emoji} <b>{swap['side']}</b>\n"
        f"💰 {amount}\n"
        f"⚡ {eth}\n"
        f"🏷 {swap.get('price_usd', '?') or '…'} USD\n"
        f"🔗 <a href=\"https://etherscan.io/tx/{swap['tx_hash']}\">View</a>"
    )


async def process_swaps():
    """Poll → deduplicate → persist → announce."""
    engine = get_engine()
    session = get_session(engine)
    bot = Bot(token=FEED_BOT_TOKEN)

    swaps = await fetch_recent_swaps()
    new_count = 0

    for sw in swaps:
        exists = session.scalar(
            select(SwapEvent).where(SwapEvent.tx_hash == sw["tx_hash"])
        )
        if exists:
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

        if FEED_CHANNEL_ID:
            try:
                await bot.send_message(
                    chat_id=FEED_CHANNEL_ID,
                    text=format_swap(sw),
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                record.notified = True
                record.notified_at = datetime.now(timezone.utc)
                new_count += 1
            except TelegramError as e:
                log.error("Telegram send failed for tx %s: %s", sw["tx_hash"], e)

    if new_count:
        session.commit()
        log.info("📊 %d new swaps announced (%d polled)", new_count, len(swaps))
    else:
        log.debug("No new swaps this poll")

    session.close()
    engine.dispose()


# ── Entry ───────────────────────────────────────────────

async def main():
    log.info("📈 Buy-Feed Bot starting — polling every %ds", POLL_INTERVAL)

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
    asyncio.run(main())