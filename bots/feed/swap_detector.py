"""
Swap detection for COPE/ETH Uniswap V4 pool.

Sources:
  1. DexScreener orders API — real-time swap data
  2. Alchemy Transfers API — ERC-20 Transfer events on-chain (fallback)

Detection logic:
  - V4 uses hook contracts rather than a direct pool address
  - Any large COPE transfer from a real wallet = BUY (user buying COPE)
  - Any large COPE transfer to a real wallet = SELL (user selling COPE)
  - Post all BUY and SELL swaps above dust threshold

Environment variables:
  COPE_TOKEN_ADDRESS  — ERC-20 contract address
  ALCHEMY_API_KEY     — Alchemy HTTP endpoint key
  BUY_MIN_USD         — Minimum USD value to post (default: $50)
  DEX_PAIR_ADDRESS    — DexScreener pair address for primary detection
"""

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import requests
from sqlalchemy import select
from shared.models import SwapEvent

log = logging.getLogger("feed.swap_detector")

COINGECKO_API  = "https://api.coingecko.com/api/v3/simple/price"
DEX_PAIR_ADDR  = os.environ.get(
    "DEX_PAIR_ADDRESS",
    "0x5503eb5f50081c50e32bc6aa75413442df581f85a3dff3184f41ce3b21c01688",
)
CACHE_TTL      = 60  # seconds

_price_cache: Optional[float] = None
_price_cache_at: float = 0


# ── ETH price ─────────────────────────────────────────

def _eth_price() -> float:
    """ETH/USD from Coingecko, cached for CACHE_TTL seconds."""
    global _price_cache, _price_cache_at
    import time
    now = time.monotonic()
    if _price_cache and (now - _price_cache_at) < CACHE_TTL:
        return _price_cache

    try:
        resp = requests.get(
            COINGECKO_API,
            params={"ids": "ethereum", "vs_currencies": "usd"},
            timeout=10,
        )
        resp.raise_for_status()
        _price_cache = float(resp.json()["ethereum"]["usd"])
        _price_cache_at = now
        return _price_cache
    except Exception as e:
        log.warning("Coingecko failed: %s — fallback 3000", e)
        return 3000.0


# ── DexScreener primary source ───────────────────────

def _dexswaps() -> list[dict]:
    """
    Pull recent swaps from DexScreener orders endpoint.
    Returns swap dicts: {tx_hash, block_number, timestamp, side,
    amount_cope, amount_eth, price_usd, maker}.
    """
    url = f"https://api.dexscreener.com/orders/v1/ethereum/{DEX_PAIR_ADDR}"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        raw = resp.json()
    except Exception as e:
        log.warning("DexScreener %s: %s", url, e)
        return []

    # Parse orders response: {"orders": [...], "boosts": [...]}
    orders = raw.get("orders", []) if isinstance(raw, dict) else []
    if not orders:
        return []

    swaps = []
    for order in orders:
        try:
            side = str(order.get("type") or order.get("side") or "").upper()
            if side not in ("BUY", "SELL"):
                continue

            raw_cope = order.get("amountIn") or order.get("fromTokenAmount") or order.get("amount") or 0
            raw_eth  = order.get("quoteAmount") or order.get("toTokenAmount") or order.get("totalQuoteAmount") or 0

            if isinstance(raw_cope, str):
                raw_cope = int(raw_cope, 16) if raw_cope.startswith("0x") else int(raw_cope)
            if isinstance(raw_eth, str):
                raw_eth = int(raw_eth, 16) if raw_eth.startswith("0x") else int(raw_eth)

            amount_cope = float(raw_cope) / 1e18
            amount_eth  = float(raw_eth)  / 1e18

            if amount_cope <= 0:
                continue

            tx_hash = (
                order.get("txHash") or order.get("tx_hash")
                or order.get("hash") or order.get("transactionHash") or ""
            )
            if len(tx_hash) < 10:
                continue

            ts_ms = int(order.get("timestamp") or order.get("blockTimestamp") or 0)
            if ts_ms > 1e12:
                pass  # milliseconds already
            elif ts_ms > 1e9:
                ts_ms *= 1000
            timestamp = (
                datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
                if ts_ms else datetime.now(timezone.utc)
            )

            swaps.append({
                "tx_hash":      tx_hash,
                "block_number": int(order.get("blockNumber") or 0),
                "timestamp":    timestamp,
                "pair":         "COPE/ETH",
                "side":         side,
                "amount_cope":  amount_cope,
                "amount_eth":   amount_eth,
                "price_usd":    0.0,
                "maker":        order.get("maker") or order.get("from") or "",
            })
        except (ValueError, TypeError, KeyError) as e:
            log.debug("Malformed DexScreener order: %s", e)
            continue

    if swaps:
        log.info("DexScreener: %d swaps", len(swaps))
    return swaps


# ── Alchemy Transfers fallback ─────────────────────────

def _alchemy_swaps(session) -> list[dict]:
    """
    Scan COPE ERC-20 Transfer events via Alchemy.

    Strategy for Uniswap V4:
      - V4 uses hook contracts, not a direct pool address
      - Track ALL large COPE transfers, ignore mints and deployer
      - Resume from the highest block_number stored in the DB
        (survives Railway redeploys / ephemeral filesystem)
    """
    api_key = os.environ.get("ALCHEMY_API_KEY", "")
    if not api_key:
        log.warning("ALCHEMY_API_KEY not set — Alchemy swap detection disabled")
        return []

    COPE_TOKEN = os.environ.get("COPE_TOKEN_ADDRESS", "0xfde746de4bfac84163580e3d568366d4bc53358a")
    if not COPE_TOKEN or COPE_TOKEN.startswith("0x..."):
        return []

    # Resume from the highest stored block number in the DB
    last_record = session.scalars(
        select(SwapEvent.block_number)
        .where(SwapEvent.block_number > 0)
        .order_by(SwapEvent.block_number.desc())
        .limit(1)
    ).first()
    from_block = last_record or 0x18d06ff  # deployment block fallback

    url = f"https://eth-mainnet.g.alchemy.com/v2/{api_key}"

    # Get latest block
    try:
        resp = requests.post(url, json={
            "id": 1, "jsonrpc": "2.0", "method": "eth_blockNumber", "params": []
        }, timeout=10)
        latest_block = int(resp.json()["result"], 16)
    except Exception as e:
        log.warning("Failed to get latest block: %s", e)
        return []

    if from_block >= latest_block:
        log.debug("No new blocks to scan (%d >= %d)", from_block, latest_block)
        return []

    # Alchemy free tier limits each request to 10 blocks — batch in windows
    WINDOW = min(latest_block - from_block, 1000)  # cap at 1000 to avoid too many requests
    scan_from = max(from_block, latest_block - WINDOW)
    log.info("Scanning blocks %s → %s (%d blocks in batches of 10)",
             hex(scan_from), hex(latest_block), WINDOW)

    all_transfers = []
    for batch_start in range(scan_from, latest_block, 10):
        batch_end = min(batch_start + 10, latest_block)
        body = {
            "id": 1, "jsonrpc": "2.0", "method": "alchemy_getAssetTransfers",
            "params": [{
                "fromBlock": hex(batch_start),
                "toBlock":   hex(batch_end),
                "contractAddresses": [COPE_TOKEN],
                "category": ["erc20"],
            }],
        }
        try:
            resp = requests.post(url, json=body, timeout=20)
            resp.raise_for_status()
            batch = resp.json().get("result", {}).get("transfers", [])
            all_transfers.extend(batch)
        except Exception as e:
            log.debug("Alchemy batch %s-%s failed: %s", hex(batch_start), hex(batch_end), e)
            continue

    transfers = all_transfers

    ZERO_ADDR = "0x0000000000000000000000000000000000000000"
    DEPLOYER  = "0x000000000004444c5dc75cb358380d2e3de08a90".lower()
    MIN_COPE  = 100

    swaps = []
    seen_hashes = set()

    for t in transfers:
        try:
            h = t.get("hash", "")
            if not h or h in seen_hashes:
                continue
            seen_hashes.add(h)

            from_addr = t.get("from", "").lower()

            if from_addr == ZERO_ADDR or from_addr == DEPLOYER:
                continue

            val_raw = t.get("value", 0)
            try:
                value_cope = float(val_raw)
            except (ValueError, TypeError):
                continue

            if value_cope < MIN_COPE:
                continue

            block = int(t.get("blockNum", 0))
            raw_ts = t.get("metadata", {}).get("blockTimestamp", "")
            try:
                timestamp = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
            except Exception:
                timestamp = datetime.now(timezone.utc)

            swaps.append({
                "tx_hash":      h,
                "block_number": block,
                "timestamp":    timestamp,
                "pair":         "COPE/ETH",
                "side":         "BUY",
                "amount_cope":  value_cope,
                "amount_eth":   0.0,
                "price_usd":    0.0,
                "maker":        from_addr,
            })

        except (ValueError, TypeError, KeyError):
            log.debug("Skipping malformed transfer: %s", t)
            continue

    if swaps:
        log.info("Alchemy: %d new swaps detected", len(swaps))
    return swaps


# ── Main export ────────────────────────────────────────

async def fetch_recent_swaps(session) -> list[dict]:
    """
    Fetch recent COPE swaps from primary (DexScreener) and fallback (Alchemy).
    Deduplicates by tx_hash.
    """
    eth_price = _eth_price()
    all_swaps = []

    # 1. DexScreener primary
    dex = _dexswaps()
    # 2. Alchemy fallback (always run, DexScreener only has 24h window)
    alchemy = _alchemy_swaps(session)
        for s in alchemy:
            s["price_usd"] = eth_price
        all_swaps.extend(alchemy)

    # Deduplicate
    seen, unique = set(), []
    for s in all_swaps:
        if s["tx_hash"] not in seen:
            seen.add(s["tx_hash"])
            unique.append(s)

    log.info("Total unique swaps to process: %d", len(unique))
    return unique
