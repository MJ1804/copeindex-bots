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
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import desc, func, select
from telegram import Bot, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

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
        log.exception("Failed to record message")
        session.rollback()
    finally:
        session.close()
        engine.dispose()


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Test — should reply instantly without any DB calls."""
    log.info("cmd_ping called from %s", update.effective_user)
    try:
        await update.message.reply_text("🏓 pong — grind bot alive")
    except Exception as e:
        log.exception("ping reply failed: %s", e)


async def cmd_rank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /rank — show current week top 10."""
    log.info("cmd_rank called from %s in chat %s", update.effective_user, update.effective_chat)
    if not update.effective_chat:
        log.error("cmd_rank: no effective_chat")
        return

    try:
        engine = get_engine()
        session = get_session(engine)
    except Exception as e:
        log.exception("cmd_rank: DB connect failed: %s", e)
        return

    week = week_label()
    log.info("cmd_rank: querying week=%s", week)
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
    log.info("cmd_rank: got %d rows", len(rows))

    if not rows:
        text = (
            f"🏆 <b>Weekly Leaderboard</b> — {week}\n\n"
            f"<i>No messages yet this week. Say something!</i>"
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


async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/leaderboard — alias for /rank."""
    await cmd_rank(update, context)


# ── Rules ───────────────────────────────────────────────

async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /rules."""
    if not update.effective_chat:
        return
    text = (
        "🎮 <b>Grind Leaderboard — Rules</b>\n\n"
        "Every message earns points based on quality:\n"
        "• 1 pt — any message\n"
        "• +1 pt — message over 140 chars\n"
        "• +2 pts — message over 500 chars\n"
        "• +1 pt — reply to another user\n"
        "• +1 pt — includes photo/sticker/media\n"
        "• Max 5 pts per message\n\n"
        "<b>Commands:</b>\n"
        "/rank — view this week's leaderboard\n"
        "/leaderboard — same as /rank\n"
        "/rules — this message\n\n"
        "<i>Leaderboard resets every Sunday at 00:10 UTC.</i>"
    )
    await update.message.reply_text(
        text, parse_mode="HTML", disable_web_page_preview=True
    )


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
        winner = rows[0]
        lines = [
            f"🏆 <b>WEEK {last_week} WINNER</b>\n"
            f"@{winner.username} — {winner.total} pts\n\n"
            f"📊 <b>Final Leaderboard</b>\n",
        ]
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
    # Command handlers MUST be registered BEFORE MessageHandler —
    # otherwise /commands get swallowed as regular text messages.
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("rank", cmd_rank))
    app.add_handler(CommandHandler("leaderboard", cmd_leaderboard))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND | filters.PHOTO | filters.VIDEO | filters.Sticker.ALL | filters.Document.ALL,
        handle_message,
    ))

    # Weekly leaderboard every Sunday at 00:10 UTC
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(weekly_leaderboard_post, "cron", day_of_week="sun", hour=0, minute=10)
    scheduler.start()

    # Start manually instead of run_polling() — avoids signal-handler
    # registration that crashes in non-main threads on Python 3.13.
    await app.initialize()
    await app.updater.start_polling(drop_pending_updates=True)
    await app.start()
    log.info("Polling for messages + /rank command + weekly post ready")

    # Keep alive until cancelled
    stop = asyncio.Event()
    try:
        await stop.wait()
    except asyncio.CancelledError:
        pass
    finally:
        await app.stop()
        await app.updater.stop()
        await app.shutdown()
        log.info("Grind bot stopped")


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(main())
    loop.close()
