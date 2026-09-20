"""
Swap detection for COPE/ETH Uniswap V4 pool.

Sources (in priority order):
  1. DexScreener orders API — live swap data, no API key needed
  2. Alchemy Transfers API — ERC-20 Transfer events on-chain (backup)

Detection logic:
  - DexScreener returns BUY/SELL directly per tx, with COPE amount + ETH amount
  - Alchemy Transfer events: COPE transfer TO pool = BUY, FROM pool = SELL

Environment variables:
  COPE_TOKEN_ADDRESS  — ERC-20 contract address (used by Alchemy fallback)
  UNISWAP_POOL        — Uniswap pool address (used by Alchemy fallback)
  ALCHEMY_API_KEY     — Alchemy HTTP endpoint key
  DEX_PAIR_ADDRESS    — DexScreener pair address (default: COPE/ETH primary)
"""

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import requests

from shared.config import COPE_TOKEN_ADDRESS, UNISWAP_POOL

log = logging.getLogger("feed.swap_detector")

COINGECKO_API    = "https://api.coingecko.com/api/v3/simple/price"
DEXSCRAPER_PAIR  = os.environ.get(
    "DEX_PAIR_ADDRESS",
    "0x5503eb5f50081c50e32bc6aa75413442df581f85a3dff3184f41ce3b21c01688",
)
CACHE_TTL = 60  # seconds

_price_cache: Optional[float] = None
_price_cache_at: float = 0


# ── ETH price ─────────────────────────────────────────

def _get_eth_price() -> float:
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
        data = resp.json()
        _price_cache = float(data["ethereum"]["usd"])
        _price_cache_at = now
        return _price_cache
    except Exception as e:
        log.warning("Coingecko failed: %s — using fallback 3000", e)
        return 3000.0


# ── DexScreener ──────────────────────────────────────

def _fetch_dexswaps() -> list[dict]:
    """
    Pull recent swaps from DexScreener orders endpoint.
    Returns normalised swap dicts: {tx_hash, block_number, timestamp, side,
    amount_cope, amount_eth, price_usd, maker}.
    """
    url = (
        f"https://api.dexscreener.com/latest/dex/tokens/{COPE_TOKEN_ADDRESS}"
    )
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning("DexScreener request failed: %s", e)
        return []

    # Accept {pairs: [{..., txns: {h1: {buys:[], sells:[]}}}]}
    if isinstance(data, dict):
        pairs = data.get("pairs", [])
        txs = []
        # Each pair may have a swap history — extract most recent
        for pair in pairs:
            pair_txs = pair.get("txns", {})
            for period in ("h1", "h6", "h24"):
                period_data = pair_txs.get(period, {})
                for tx in period_data.get("buys", []) + period_data.get("sells", []):
                    tx["_pair_address"] = pair.get("pairAddress", "")
                    txs.append(tx)
    elif isinstance(data, list):
        txs = data
    else:
        return []

    swaps = []
    for tx in txs:
        try:
            side = str(tx.get("type") or tx.get("side") or "").upper()
            if side not in ("BUY", "SELL"):
                continue

            # Amounts: DexScreener returns raw integers (no decimals for base token)
            raw_cope = tx.get("amountIn") or tx.get("fromTokenAmount") or tx.get("amount") or 0
            raw_eth  = tx.get("quoteAmount") or tx.get("toTokenAmount") or tx.get("totalQuoteAmount") or 0

            # Try string -> int
            if isinstance(raw_cope, str):
                raw_cope = int(raw_cope, 0) if raw_cope.startswith("0x") else int(raw_cope)
            if isinstance(raw_eth, str):
                raw_eth = int(raw_eth, 0) if raw_eth.startswith("0x") else int(raw_eth)

            amount_cope = float(raw_cope) / 1e18  # COPE has 18 decimals
            amount_eth  = float(raw_eth)  / 1e18  # ETH has 18 decimals

            if amount_cope <= 0:
                continue

            tx_hash = (
                tx.get("txHash") or tx.get("tx_hash") or tx.get("hash")
                or tx.get("transactionHash") or ""
            )
            if len(tx_hash) < 10:
                continue

            block = int(tx.get("blockNumber") or 0)

            ts_ms = int(tx.get("timestamp") or tx.get("blockTimestamp") or 0)
            if ts_ms > 1e12:
                ts_ms = ts_ms  # milliseconds
            elif ts_ms > 1e9:
                ts_ms = ts_ms * 1000
            timestamp = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc) if ts_ms else datetime.now(timezone.utc)

            maker = tx.get("maker") or tx.get("from") or tx.get("wallet") or ""

            swaps.append({
                "tx_hash":      tx_hash,
                "block_number": block,
                "timestamp":    timestamp,
                "pair":         "COPE/ETH",
                "side":         side,
                "amount_cope":  amount_cope,
                "amount_eth":   amount_eth,
                "price_usd":    0.0,   # filled below
                "maker":         maker,
            })
        except (ValueError, TypeError, KeyError) as e:
            log.debug("Malformed DexScreener tx: %s — %s", e, tx)
            continue

    if swaps:
        log.info("DexScreener: %d raw swaps fetched", len(swaps))
    return swaps


# ── Alchemy Transfers (backup) ─────────────────────────

def _alchemy_transfers() -> list[dict]:
    """
    Scan Alchemy Transfer events on the COPE token.
    Transfer TO pool = BUY, Transfer FROM pool = SELL.
    """
    api_key = os.environ.get("ALCHEMY_API_KEY", "")
    if not api_key:
        return []

    # Skip if addresses are placeholders
    if not COPE_TOKEN_ADDRESS or COPE_TOKEN_ADDRESS.startswith("0x..."):
        return []
    pool_addr = os.environ.get("UNISWAP_POOL") or ""
    if not pool_addr or pool_addr.startswith("0x..."):
        pool_addr = ""

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

    WINDOW = 20  # ~5 min at 12s blocks
    from_block = max(1, latest_block - WINDOW)

    # Scan all COPE transfers, then filter by pool address for side
    body = {
        "id": 1, "jsonrpc": "2.0", "method": "alchemy_getAssetTransfers",
        "params": [{
            "fromBlock": hex(from_block),
            "toBlock":   "latest",
            "contractAddresses": [COPE_TOKEN_ADDRESS],
            "category":   ["erc20"],
            "withABI":    True,
        }],
    }

    try:
        resp = requests.post(url, json=body, timeout=15)
        resp.raise_for_status()
        transfers = resp.json().get("result", {}).get("transfers", [])
    except Exception as e:
        log.warning("Alchemy Transfers API failed: %s", e)
        return []

    swaps = []
    for tx in transfers:
        try:
            raw_val = tx.get("value", 0)
            if isinstance(raw_val, str):
                value = int(raw_val, 16) if raw_val.startswith("0x") else int(raw_val)
            else:
                value = int(raw_val) if raw_val else 0

            amount_cope = value / 1e18
            if amount_cope < 100:
                continue

            tx_hash = tx.get("hash", "")
            block_num = tx.get("blockNum", "0")
            block = int(block_num, 16) if isinstance(block_num, str) else int(block_num)

            raw_ts = tx.get("metadata", {}).get("blockTimestamp", "")
            try:
                timestamp = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
            except Exception:
                timestamp = datetime.now(timezone.utc)

            from_addr = tx.get("from", "")
            to_addr   = tx.get("to",   "")

            # side: BUY = anyone sending COPE TO the pool (excluding the token contract itself)
            side = "BUY" if to_addr.lower() == pool_addr.lower() and from_addr.lower() != COPE_TOKEN_ADDRESS.lower() else "SELL"

            swaps.append({
                "tx_hash":      tx_hash,
                "block_number": block,
                "timestamp":    timestamp,
                "pair":         "COPE/ETH",
                "side":         side,
                "amount_cope":  amount_cope,
                "amount_eth":   0.0,
                "price_usd":    0.0,
                "maker":        from_addr,
            })
        except (ValueError, TypeError, KeyError):
            continue

    if swaps:
        log.info("Alchemy: %d COPE transfer events", len(swaps))
    return swaps


# ── Main export ────────────────────────────────────────

async def fetch_recent_swaps() -> list[dict]:
    """
    Fetch recent COPE/ETH swaps from primary and backup sources.
    Returns deduplicated list of swap dicts.
    """
    eth_price = _get_eth_price()
    all_swaps = []

    # 1. DexScreener primary
    dex_swaps = _fetch_dexswaps()
    for s in dex_swaps:
        s["price_usd"] = eth_price
    all_swaps.extend(dex_swaps)

    # 2. Alchemy backup
    if not dex_swaps:
        # Only use Alchemy if DexScreener returned nothing
        alchemy_swaps = _alchemy_transfers()
        for s in alchemy_swaps:
            s["price_usd"] = eth_price
        all_swaps.extend(alchemy_swaps)

    # Deduplicate by tx_hash (prefer DexScreener — has better data)
    seen = set()
    unique = []
    for s in all_swaps:
        if s["tx_hash"] not in seen:
            seen.add(s["tx_hash"])
            unique.append(s)

    if unique:
        log.info("Total unique swaps: %d", len(unique))
    return unique
