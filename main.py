#!/usr/bin/env python3
"""
CopeIndex — unified single-process entry point.
Runs the public HTTP API (Flask, threaded) and all three Telegram bots concurrently.
"""

import asyncio
import logging
import os
import threading

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [copeindex] %(levelname)s %(message)s",
)
log = logging.getLogger("copeindex")


def run_api():
    """Start Flask in a daemon thread so it doesn't block the asyncio loop."""
    from api_server import app
    port = int(os.environ.get("PORT", "8000"))
    log.info(f"API starting on 0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False, threaded=True)


async def main():
    log.info("CopeIndex booting — API + bots")

    # Start Flask API in background thread
    t = threading.Thread(target=run_api, name="api", daemon=True)
    t.start()
    await asyncio.sleep(1)  # Give Flask a moment to bind
    log.info("API thread started")

    tasks = []
    for label, fn, env_key in [
        ("crown", "bots.crown.bot", "CROWN_BOT_TOKEN"),
        ("feed",  "bots.feed.bot",  "FEED_BOT_TOKEN"),
        ("grind", "bots.grind.bot", "GRIND_BOT_TOKEN"),
    ]:
        if os.environ.get(env_key):
            mod = __import__(fn, fromlist=["main"])
            if label == "grind":
                # Grind bot uses telegram Application.run_polling() which breaks
                # shared event loops — run it in its own thread + loop
                def _run_grind(mod=mod):
                    grind_loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(grind_loop)
                    try:
                        grind_loop.run_until_complete(mod.main())
                    except Exception:
                        import traceback
                        traceback.print_exc()
                    finally:
                        grind_loop.close()
                log.info("✅ grind registered (threaded)")
                t = threading.Thread(target=_run_grind, name="grind", daemon=True)
                t.start()
            else:
                log.info(f"✅ {label} registered")
                tasks.append(asyncio.create_task(mod.main(), name=label))
        else:
            log.warning(f"⚠️  {label} skipped — no token")

    if not tasks:
        log.error("No bots configured — nothing to do")
        return

    log.info(f"Running {len(tasks)} bot(s) + API + grind(threaded)")
    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        log.info("Shutdown signal received")
    except Exception:
        log.exception("Fatal error")


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()
        log.info("CopeIndex stopped")
