"""
Grind Leaderboard Bot — chat participation points system.

Architecture:
  - Listens to Telegram group chat messages via polling (getUpdates)
  - Scores each message (1-5 points based on length, reply depth, media)
  - Stores in chat_messages table
  - Weekly leaderboard posted on Sunday 00:10 UTC
  - /rank inline command for current standings

Point system (tunable):
  - Base: 1 point per message
  - +1 if >140 chars
  - +2 if >500 chars
  - +1 if reply to another user (engagement)
  - +1 if contains media/photo/sticker
  - Max 5 points per message
"""

import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select, func, desc
from telegram import Bot, Update
from telegram.error import TelegramError, RetryAfter
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

from shared.config import GRIND_BOT_TOKEN, GRIND_CHANNEL_ID
from shared.models import ChatMessage, get_engine, get_session

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [grind] %(levelname)s %(message)s",
)
log = logging.getLogger("grind")

RATE_LIMIT_DELAY = float(os.environ.get("GRIND_RANK_DELAY", "0.05"))

# Minimum user_id — Telegram IDs below this are from before ~2016.
# Fresh accounts have IDs in the billions; set higher to gate by account age.
# Default ~500M = roughly 2016. Raise to ~5B for accounts created after ~2020.
MIN_USER_ID = int(os.environ.get("GRIND_MIN_USER_ID", "500_000_000"))


# ── Scoring ─────────────────────────────────────────────

def score_message(text: str, is_reply: bool, has_media: bool) -> int:
    """Calculate points for a single message."""
    points = 1
    if text:
        length = len(text)
        if length > 500:
            points += 2
        elif length > 140:
            points += 1
    if is_reply:
        points += 1
    if has_media:
        points += 1
    return min(points, 5)


def week_label(dt: datetime | None = None) -> str:
    """ISO week label, e.g. 2026-W38."""
    dt = dt or datetime.now(timezone.utc)
    iso = dt.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


# ── Telegram handlers ───────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Score every message in the group."""
    if not update.message or not update.effective_user:
        return

    msg = update.message
    user = update.effective_user

    # ── Sybil gate: username required ──
    if not user.username:
        log.debug("ignored %s — no username set", user.id)
        return

    # ── Sybil gate: account age (user_id ≈ creation date) ──
    if user.id < MIN_USER_ID:
        log.debug("ignored %s — account too new (id=%d < %d)",
                  user.id, user.id, MIN_USER_ID)
        return

    engine = get_engine()
    session = get_session(engine)

    try:
        record = ChatMessage(
            chat_id=str(msg.chat_id),
            user_id=str(user.id),
            username=user.username or user.full_name,
            message_id=msg.message_id,
            text_length=len(msg.text or msg.caption or ""),
            is_reply=(msg.reply_to_message is not None),
            has_media=(msg.photo or msg.video or msg.sticker or msg.document),
            posted_at=msg.date.replace(tzinfo=timezone.utc),
            points=score_message(
                msg.text or msg.caption or "",
                msg.reply_to_message is not None,
                bool(msg.photo or msg.video or msg.sticker or msg.document),
            ),
            weekly_period=week_label(msg.date),
        )
        session.add(record)
        session.commit()
        log.debug("+%d pt → %s", record.points, record.username)
    except Exception:
        session.rollback()
    finally:
        session.close()
        engine.dispose()


async def cmd_rank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /rank — show current week top 10."""
    if not update.effective_chat:
        return

    engine = get_engine()
    session = get_session(engine)

    week = week_label()
    rows = session.execute(
        select(
            ChatMessage.username,
            func.sum(ChatMessage.points).label("total"),
            func.count(ChatMessage.id).label("msgs"),
        )
        .where(ChatMessage.weekly_period == week)
        .group_by(ChatMessage.user_id, ChatMessage.username)
        .order_by(desc("total"))
        .limit(10)
    ).all()

    if not rows:
        text = "🏆 <b>Weekly Leaderboard</b> — {}\n\n<i>No messages yet this week. Say something!</i>".format(
            week
        )
    else:
        lines = [f"🏆 <b>Weekly Leaderboard</b> — {week}\n"]
        medals = ["🥇", "🥈", "🥉"]
        for i, row in enumerate(rows):
            prefix = medals[i] if i < 3 else f"{i+1}."
            lines.append(
                f"{prefix} @{row.username} — <b>{row.total} pts</b> ({row.msgs} msgs)"
            )

        text = "\n".join(lines)

    try:
        await asyncio.sleep(RATE_LIMIT_DELAY)
        await update.message.reply_text(text, parse_mode="HTML",
                                        disable_web_page_preview=True)
    except TelegramError as e:
        log.error("rank reply failed: %s", e)
    finally:
        session.close()
        engine.dispose()


# ── Weekly post ─────────────────────────────────────────

async def weekly_leaderboard_post():
    """Fire at 00:10 UTC Sunday — post top 10 to the channel."""
    engine = get_engine()
    session = get_session(engine)
    bot = Bot(token=GRIND_BOT_TOKEN)

    last_week = week_label(datetime.now(timezone.utc) - timedelta(days=7))
    rows = session.execute(
        select(
            ChatMessage.username,
            func.sum(ChatMessage.points).label("total"),
            func.count(ChatMessage.id).label("msgs"),
        )
        .where(ChatMessage.weekly_period == last_week)
        .group_by(ChatMessage.user_id, ChatMessage.username)
        .order_by(desc("total"))
        .limit(10)
    ).all()

    if rows and GRIND_CHANNEL_ID:
        lines = [f"🏆 <b>WEEK {last_week} WINNER</b>\n@{rows[0].username} — {rows[0].total} pts\n\n📊 <b>Final Leaderboard</b>\n"]
        medals = ["🥇", "🥈", "🥉"]
        for i, row in enumerate(rows):
            prefix = medals[i] if i < 3 else f"{i+1}."
            lines.append(
                f"{prefix} @{row.username} — <b>{row.total} pts</b> ({row.msgs} msgs)"
            )
        text = "\n".join(lines) + "\n\n🏆 <b>Winner, DM @ReadKearns to claim.</b>"

        try:
            await bot.send_message(
                chat_id=GRIND_CHANNEL_ID,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            log.info("📣 Weekly leaderboard posted — %s", last_week)
        except TelegramError as e:
            log.error("Weekly post failed: %s", e)

    session.close()
    engine.dispose()


# ── Entry ───────────────────────────────────────────────

async def main():
    log.info("🎮 Grind Leaderboard Bot starting")

    engine = get_engine()
    from shared.models import create_all
    create_all(engine)
    engine.dispose()

    app = Application.builder().token(GRIND_BOT_TOKEN).build()
    app.add_handler(MessageHandler(
        filters.TEXT | filters.PHOTO | filters.VIDEO | filters.Sticker.ALL | filters.Document.ALL,
        handle_message,
    ))
    app.add_handler(CommandHandler("rank", cmd_rank))

    # Weekly leaderboard every Sunday at 00:10 UTC
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(weekly_leaderboard_post, "cron", day_of_week="sun", hour=0, minute=10)
    scheduler.start()

    log.info("Polling for messages + /rank command + weekly post ready")
    try:
        await app.run_polling(drop_pending_updates=True)
    except KeyboardInterrupt:
        log.info("Shutting down")


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(main())
    loop.close()