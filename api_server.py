"""
CopeIndex HTTP API — public archive endpoints.
Railway runs this alongside the bot workers.

Public routes:
  GET /health
  GET /api/crown/archive?limit=30&offset=0
  GET /api/burns?limit=30&offset=0
  GET /api/transparency
"""

import logging
import os

from flask import Flask, jsonify, request
from flask_cors import CORS
from sqlalchemy import func, select

from shared.models import CrownWinner, SwapEvent, get_engine, get_session

app = Flask(__name__)
CORS(app)

log = logging.getLogger("api")


def json_crown(winner):
    """Serialize a CrownWinner row to a dict for JSON."""
    return {
        "id": winner.id,
        "date": winner.date.isoformat() if winner.date else None,
        "userId": winner.user_id,
        "username": winner.username,
        "messageText": winner.message_text,
        "totalCope": winner.total_cope,
        "burnedCope": winner.burned_cope,
        "txHash": winner.tx_hash,
        "announcedAt": winner.announced_at.isoformat() if winner.announced_at else None,
    }


def json_swap(swap):
    """Serialize a SwapEvent row to a dict for JSON."""
    return {
        "id": swap.id,
        "txHash": swap.tx_hash,
        "blockNumber": swap.block_number,
        "timestamp": swap.timestamp.isoformat() if swap.timestamp else None,
        "pair": swap.pair,
        "side": swap.side,
        "amountCope": swap.amount_cope,
        "amountEth": swap.amount_eth,
        "priceUsd": swap.price_usd,
        "maker": swap.maker,
    }


# ── Health ──────────────────────────────────────────────

@app.route("/health")
def health():
    try:
        engine = get_engine()
        session = get_session(engine)
        session.execute(select(1))
        session.close()
        engine.dispose()
        return jsonify({"status": "ok", "db": "connected"})
    except Exception as e:
        log.error("Health check failed: %s", e)
        return jsonify({"status": "ok", "db": "disconnected"}), 200


# ── Crown Archive ──────────────────────────────────────

@app.route("/api/crown/archive")
def crown_archive():
    try:
        limit = min(max(int(request.args.get("limit", 30)), 1), 100)
        offset = max(int(request.args.get("offset", 0)), 0)

        engine = get_engine()
        session = get_session(engine)

        # Total count
        total = session.scalar(select(func.count()).select_from(CrownWinner))

        # Paginated results
        rows = (
            session.execute(
                select(CrownWinner)
                .order_by(CrownWinner.date.desc())
                .offset(offset)
                .limit(limit)
            )
            .scalars()
            .all()
        )

        session.close()
        engine.dispose()

        return jsonify({
            "success": True,
            "total": total or 0,
            "limit": limit,
            "offset": offset,
            "hasMore": (offset + len(rows)) < (total or 0),
            "records": [json_crown(w) for w in rows],
        })
    except Exception as e:
        log.exception("crown_archive failed")
        return jsonify({"success": False, "error": str(e)}), 500


# ── Burns ──────────────────────────────────────────────

@app.route("/api/burns")
def burns():
    """Return COPE burn/swap events."""
    try:
        limit = min(max(int(request.args.get("limit", 30)), 1), 100)
        offset = max(int(request.args.get("offset", 0)), 0)

        engine = get_engine()
        session = get_session(engine)

        # Total count
        total = session.scalar(select(func.count()).select_from(SwapEvent))

        # Paginated results — newest first
        rows = (
            session.execute(
                select(SwapEvent)
                .order_by(SwapEvent.timestamp.desc())
                .offset(offset)
                .limit(limit)
            )
            .scalars()
            .all()
        )

        session.close()
        engine.dispose()

        return jsonify({
            "success": True,
            "total": total or 0,
            "limit": limit,
            "offset": offset,
            "hasMore": (offset + len(rows)) < (total or 0),
            "records": [json_swap(s) for s in rows],
        })
    except Exception as e:
        log.exception("burns failed")
        return jsonify({"success": False, "error": str(e)}), 500


# ── Transparency ────────────────────────────────────────

@app.route("/api/transparency")
def transparency():
    """
    Live COPE token transparency metrics.
    Stubbed until Phase 8 (mainnet launch).
    """
    return jsonify({
        "success": True,
        "message": "Transparency metrics will be available after COPE mainnet launch (Phase 8)",
        "stub": True,
        "estimatedSupply": 1_000_000_000,
        "totalBurned": 0,
        "circulatingEstimate": 1_000_000_000,
    })


# ── Entry ───────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [api] %(levelname)s %(message)s",
    )
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
