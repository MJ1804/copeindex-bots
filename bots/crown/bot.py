"""
Crown Announcer Bot — posts daily Crown winner at 00:05 UTC.

Architecture:
  - APScheduler fires at 00:05 UTC
  - Picks the highest-engagement message from the previous 24h
  - Records winner in crown_winners table
  - Posts formatted announcement to configured Telegram channel

Phase 8 integration:
  - Auto-burns COPE via contract call (placeholder until deploy)
  - Writes burn tx_hash back to DB
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select, func, desc
from telegram import Bot
from telegram.error import TelegramError

from shared.config import CROWN_BOT_TOKEN, CROWN_CHANNEL_ID
from shared.models import CrownWinner, ChatMessage, get_engine, get_session

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [crown] %(levelname)s %(message)s",
)
log = logging.getLogger("crown")


# ── Winner selection logic ──────────────────────────────

async def pick_daily_winner(db_session) -> dict:
    """
    Select today's Crown winner.

    Crown goes to whoever topped the grind leaderboard for the current UTC day.
    Queries chat_messages for the highest cumulative points since 00:00 UTC.
    Falls back to a placeholder if no messages were recorded.
    """
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    # Check if already awarded today
    existing = db_session.scalar(
        select(CrownWinner).where(CrownWinner.date >= today).limit(1)
    )
    if existing:
        log.info("Crown already awarded today → %s", existing.username)
        return None

    # Find top scorer from today's grind messages
    top = db_session.execute(
        select(
            ChatMessage.user_id,
            ChatMessage.username,
            func.sum(ChatMessage.points).label("total"),
            func.count(ChatMessage.id).label("msgs"),
        )
        .where(ChatMessage.posted_at >= today)
        .group_by(ChatMessage.user_id, ChatMessage.username)
        .order_by(desc("total"))
        .limit(1)
    ).first()

    if top:
        log.info("Top scorer today → %s with %d pts (%d msgs)",
                 top.username, top.total, top.msgs)
        return {
            "user_id": top.user_id,
            "username": top.username or top.user_id,
            "message_text": f"Topped the daily grind with {top.total} pts from {top.msgs} messages",
            "total_cope": 0,
            "burned_cope": 0,
        }

    log.info("No grind messages today — bootstrap mode")
    return {
        "user_id": "placeholder",
        "username": "anon",
        "message_text": "(no messages today — bootstrap mode)",
        "total_cope": 0,
        "burned_cope": 0,
    }


# ── Announcement ────────────────────────────────────────

def build_announcement(winner: dict) -> str:
    """Render the Crown announcement message."""
    return (
        f"👑 <b>Crown of the Day</b> — {datetime.now(timezone.utc).strftime('%B %d, %Y')}\n\n"
        f"🏆 @{winner['username']}\n"
        f"💬 {winner.get('message_text', '...')}\n\n"
        f"📩 <b>Winner, DM @ReadKearns to claim.</b>\n\n"
        f"<i>The Crown burns again tomorrow at 00:05 UTC.</i>"
    )


async def crown_job():
    """APScheduler job — fires daily at 00:05 UTC."""
    engine = get_engine()
    session = get_session(engine)
    bot = Bot(token=CROWN_BOT_TOKEN)

    try:
        winner = await pick_daily_winner(session)
        if winner is None:
            log.info("Crown already awarded; skipping.")
            return

        text = build_announcement(winner)

        # tx_hash reserved for Phase 8 if a COPE burn mechanism is added
        tx_hash = None

        # ── Persist ──
        record = CrownWinner(
            date=datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0),
            user_id=winner["user_id"],
            username=winner["username"],
            message_text=winner.get("message_text"),
            total_cope=winner["total_cope"],
            burned_cope=winner["burned_cope"],
            tx_hash=tx_hash,
        )
        session.add(record)
        session.commit()

        # ── Announce ──
        if CROWN_CHANNEL_ID:
            await bot.send_message(
                chat_id=CROWN_CHANNEL_ID,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            record.announced_at = datetime.now(timezone.utc)
            session.commit()
            log.info("📣 Crown announced → @%s", winner["username"])
        else:
            log.warning("CROWN_CHANNEL_ID not set — skipping Telegram post")

    except TelegramError as e:
        log.error("Telegram send failed: %s", e)
        session.rollback()
    except Exception:
        log.exception("Crown job crashed")
        session.rollback()
    finally:
        session.close()
        engine.dispose()


# ── Entry ───────────────────────────────────────────────

async def main():
    log.info("👑 Crown Announcer starting")

    # Ensure tables exist (idempotent)
    engine = get_engine()
    from shared.models import create_all
    create_all(engine)
    engine.dispose()

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(crown_job, "cron", hour=0, minute=5)
    scheduler.start()

    log.info("Scheduler active — next Crown at 00:05 UTC")
    try:
        while True:
            await asyncio.sleep(60)
    except KeyboardInterrupt:
        log.info("Shutting down")


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(main())
    loop.close()