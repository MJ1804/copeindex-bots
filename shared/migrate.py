"""
Migrations script — run ONCE before bots start.

Creates all tables via SQLAlchemy metadata.create_all().
Safe to run idempotently — CREATE IF NOT EXISTS is implied.

Usage:
    python3 shared/migrate.py
"""

import logging
import os
import sys

# Allow running from repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.models import get_engine, create_all

logging.basicConfig(level=logging.INFO)

def main():
    print("Migrating → Neon PostgreSQL")
    engine = get_engine()
    create_all(engine)
    engine.dispose()
    print("✓ Tables ready: crown_winners, swap_events, chat_messages")

if __name__ == "__main__":
    main()