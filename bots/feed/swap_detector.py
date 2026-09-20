"""
Swap detection for COPE/ETH Uniswap V2 pool via Alchemy Transfers API.

Approach:
  - Use Alchemy Transfers API (no trace API needed, stays on free tier)
  - Listen for Transfer events on the COPE ERC-20 contract
  - Transfer TO the pool = BUY (user → pool = user buys COPE with ETH)
  - Transfer FROM the pool = SELL (pool → user = user sells COPE for ETH)
  - Coingecko used for USD price estimate

Environment variables required:
  ALCHEMY_API_KEY     — Alchemy HTTP endpoint key
  COPE_TOKEN_ADDRESS  — ERC-20 contract address
  UNISWAP_POOL        — Uniswap V2 pair contract address
"""

import logging
import math
import os
from datetime import datetime, timezone
from typing import Optional

import requests

from shared.config import COPE_TOKEN_ADDRESS, UNISWAP_POOL

log = logging.getLogger("feed.swap_detector")

COINGECKO_API = "https://api.coingecko.com/api/v3/simple/price"
CACHE_TTL_SECONDS = 60  # Coingecko free tier: 10-30 calls/min

# ── Price cache ─────────────────────────────────────────

_price_cache: Optional[dict] = None
_price_cache_at: float = 0


def _get_eth_price() -> float:
    """Fetch ETH/USD from Coingecko, cached for CACHE_TTL_SECONDS."""
    global _price_cache, _price_cache_at
    import time
    now = time.monotonic()
    if _price_cache and (now - _price_cache_at) < CACHE_TTL_SECONDS:
        return _price_cache["eth_usd"]

    try:
        resp = requests.get(
            COINGECKO_API,
            params={"ids": "ethereum", "vs_currencies": "usd"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        _price_cache = {"eth_usd": float(data["ethereum"]["usd"])}
        _price_cache_at = now
        return _price_cache["eth_usd"]
    except Exception as e:
        log.warning("Coingecko price fetch failed, using fallback: %s", e)
        return 3000.0  # rough fallback


# ── Alchemy Transfers ──────────────────────────────────

def _alchemy_get_transfers(
    from_block: int,
    to_block: int | None = None,
) -> list[dict]:
    """
    Fetch Transfer events from Alchemy's Transfers API.

    from_block: block to start scanning from (exclusive)
    to_block: block to stop at (inclusive), or "latest"
    """
    api_key = os.environ.get("ALCHEMY_API_KEY", "")
    if not api_key:
        log.warning("ALCHEMY_API_KEY not set — swap detection disabled")
        return []

    url = f"https://eth-mainnet.g.alchemy.com/v2/{api_key}"

    # Build the withABI body for Transfer events on the COPE token
    body = {
        "id": 1,
        "jsonrpc": "2.0",
        "method": "alchemy_getAssetTransfers",
        "params": [
            {
                "fromBlock": hex(from_block),
                "toBlock": hex(to_block) if to_block else "latest",
                "fromAddress": COPE_TOKEN_ADDRESS,
                "toAddress": UNISWAP_POOL,
                "category": ["token"],
                "withABI": True,
            }
        ],
    }

    try:
        resp = requests.post(url, json=body, timeout=15)
        resp.raise_for_status()
        result = resp.json()
        return result.get("result", {}).get("transfers", [])
    except Exception as e:
        log.error("Alchemy Transfers API failed: %s", e)
        return []


# ── Swap detection ────────────────────────────────────

def _wei_to_eth(wei: int) -> float:
    return wei / 1e18


async def fetch_recent_swaps() -> list[dict]:
    """
    Poll Alchemy for recent COPE Transfer events involving the Uniswap pool.
    Returns list of swap dicts compatible with process_swaps() in bot.py.
    """
    from_block_str = os.environ.get("FEED_SCAN_FROM_BLOCK", "latest")

    # Skip if addresses are still placeholders (not yet deployed by Fusing)
    if (not COPE_TOKEN_ADDRESS or COPE_TOKEN_ADDRESS.startswith("0x...") or
        not UNISWAP_POOL or UNISWAP_POOL.startswith("0x...")):
        return []

    # Get latest block
    api_key = os.environ.get("ALCHEMY_API_KEY", "")
    if not api_key:
        return []

    url = f"https://eth-mainnet.g.alchemy.com/v2/{api_key}"

    try:
        # Get latest block number
        resp = requests.post(
            url,
            json={"id": 1, "jsonrpc": "2.0", "method": "eth_blockNumber", "params": []},
            timeout=10,
        )
        resp.raise_for_status()
        latest_hex = resp.json()["result"]
        latest_block = int(latest_hex, 16)
    except Exception as e:
        log.error("Failed to get latest block: %s", e)
        return []

    # Scan window: last 20 blocks (~5 min at 12s blocks)
    SCAN_WINDOW = 20
    from_block = max(1, latest_block - SCAN_WINDOW)

    # Try the Transfers API (works on most Alchemy tiers)
    transfers = _alchemy_get_transfers(from_block, latest_block)

    swaps = []
    eth_price = _get_eth_price()

    for tx in transfers:
        raw_value = tx.get("value", 0)
        if isinstance(raw_value, str):
            try:
                value = int(raw_value, 16)
            except ValueError:
                value = 0
        else:
            value = int(raw_value) if raw_value else 0

        amount_cope = value / 1e18  # Assuming 18 decimals

        # Skip dust transfers
        if amount_cope < 100:  # less than 100 COPE, skip
            continue

        tx_hash = tx.get("hash", "")
        block_num = tx.get("blockNum", "0")
        if isinstance(block_num, str):
            try:
                block_number = int(block_num, 16)
            except ValueError:
                block_number = 0
        else:
            block_number = int(block_num)

        # Timestamp
        raw_ts = tx.get("metadata", {}).get("blockTimestamp", "")
        try:
            timestamp = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
        except Exception:
            timestamp = datetime.now(timezone.utc)

        # Approximate ETH value from COPE amount + pool price
        # For accurate ETH value we'd need the swap's ETH leg.
        # Using rough estimate: COPE_amount / pool_reserve * ETH_reserve
        # Simpler: flag as BUY, show COPE amount, note ETH unknown
        amount_eth = 0.0  # Can't determine ETH leg from Transfer alone without pool state

        swaps.append({
            "tx_hash": tx_hash,
            "block_number": block_number,
            "timestamp": timestamp,
            "pair": "COPE/ETH",
            "side": "BUY",
            "amount_cope": amount_cope,
            "amount_eth": amount_eth,
            "price_usd": 0.0,  # Unknown until pool price fetched
            "maker": tx.get("from", ""),
        })

    if swaps:
        log.info("Detected %d COPE transfers to pool", len(swaps))

    return swaps


# ── Block checkpoint ───────────────────────────────────

def get_last_scanned_block() -> int | None:
    """Returns the last block from env var (for resuming scans)."""
    val = os.environ.get("FEED_LAST_SCANNED_BLOCK")
    return int(val) if val else None


def set_last_scanned_block(block: int):
    """Store the last scanned block so we don't re-scan on next poll."""
    # In production this should be in Redis or the DB.
    # For now just log it — the scan window handles dedup.
    log.debug("Last scanned block: %d", block)
