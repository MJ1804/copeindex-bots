"""Shared SQLAlchemy models for all three bots."""

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    Column, String, Integer, Float, DateTime, ForeignKey, Text, Boolean,
    create_engine, Index,
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

Base = declarative_base()


# ── Crown ───────────────────────────────────────────────

class CrownWinner(Base):
    __tablename__ = "crown_winners"

    id           = Column(String, primary_key=True, default=lambda: uuid4().hex)
    date         = Column(DateTime(timezone=True), nullable=False, index=True, unique=True,
                          doc="UTC date burned/won — one row per day")
    user_id      = Column(String, nullable=False)
    username     = Column(String, nullable=True)
    message_text = Column(Text, nullable=True,
                          doc="Winning message content (if chat-based Crown)")
    total_cope   = Column(Float, nullable=False, default=0,
                          doc="Total COPE supply at time of burn")
    burned_cope  = Column(Float, nullable=False, default=0,
                          doc="Amount actually burned this round")
    tx_hash      = Column(String, nullable=True,
                          doc="On-chain burn tx hash (or placeholder if manual)")
    announced_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                          doc="When the bot posted the announcement")
    extra        = Column(Text, nullable=True, doc="Arbitrary JSON blob for future fields")

    Index("idx_crown_date", date)

    def __repr__(self):
        return f"<CrownWinner {self.date.strftime('%Y-%m-%d')} {self.username}>"


# ── Buy-Feed ────────────────────────────────────────────

class SwapEvent(Base):
    __tablename__ = "swap_events"

    id          = Column(String, primary_key=True, default=lambda: uuid4().hex)
    tx_hash     = Column(String, nullable=False, unique=True, index=True)
    block_number = Column(Integer, nullable=False)
    timestamp   = Column(DateTime(timezone=True), nullable=False, index=True)

    # swap details
    pair        = Column(String, nullable=False, default="COPE/ETH",
                         doc="Token pair e.g. COPE/ETH")
    side        = Column(String, nullable=False, doc="BUY or SELL")
    amount_cope = Column(Float, nullable=False)
    amount_eth  = Column(Float, nullable=False)
    price_usd   = Column(Float, nullable=True, doc="Approx USD price at swap time")
    maker       = Column(String, nullable=True, doc="Wallet address")

    notified    = Column(Boolean, default=False,
                         doc="Telegram message sent for this swap")
    notified_at = Column(DateTime(timezone=True), nullable=True)

    Index("idx_swaps_timestamp", timestamp.desc())
    Index("idx_swaps_side", side)

    def __repr__(self):
        return f"<Swap {self.tx_hash[:10]}… {self.side} {self.amount_cope:,.0f} COPE>"


# ── Grind Leaderboard ───────────────────────────────────

class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id              = Column(String, primary_key=True, default=lambda: uuid4().hex)
    chat_id         = Column(String, nullable=False, index=True)
    user_id         = Column(String, nullable=False, index=True)
    username        = Column(String, nullable=True)
    message_id      = Column(Integer, nullable=False)
    text_length     = Column(Integer, default=0)
    is_reply        = Column(Boolean, default=False)
    has_media       = Column(Boolean, default=False)
    posted_at       = Column(DateTime(timezone=True), nullable=False, index=True,
                             doc="UTC timestamp from Telegram")
    points          = Column(Integer, default=1,
                             doc="Points scored for this message")
    weekly_period   = Column(String, nullable=False, index=True,
                             doc="ISO week label e.g. 2026-W38")

    Index("idx_chat_user_weekly", user_id, weekly_period)

    def __repr__(self):
        return f"<ChatMsg {self.username} +{self.points}>"


# ── Engine helpers ───────────────────────────────────────

def get_engine(db_url: str | None = None):
    """Create a SQLAlchemy engine. Uses DATABASE_POOLED_URL by default."""
    if db_url is None:
        from .config import DATABASE_POOLED_URL
        db_url = DATABASE_POOLED_URL
    # SQLAlchemy 2.x async-friendly — sync engine for simplicity
    return create_engine(db_url, pool_pre_ping=True, pool_recycle=300)

def create_all(engine):
    Base.metadata.create_all(engine)

def get_session(engine):
    return sessionmaker(bind=engine)()