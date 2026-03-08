"""Main entry point — asyncio orchestration, hotkey registration, shutdown."""

import asyncio
import logging
import signal
import sys

import keyboard

from config import load_settings
from polymarket import PolymarketClient
from trading import PositionTracker
from ui import Dashboard

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger(__name__)


async def async_main() -> None:
    settings = load_settings()

    print("=" * 50)
    print("  Polymarket BTC Trading Bot")
    print(f"  Market: {settings.default_market}")
    print(f"  Dry Run: {settings.dry_run}")
    print("=" * 50)
    print("Initializing...")

    # --- Phase 1: Initialize components ---
    client = PolymarketClient(settings)

    print("Discovering market...")
    market = await client.discover_market()
    print(f"  Market: {market.question}")
    print(f"  YES Token: {market.yes_token_id[:20]}...")
    print(f"  NO Token:  {market.no_token_id[:20]}...")
    print(f"  Tick Size: {market.tick_size}")
    print(f"  Neg Risk:  {market.neg_risk}")

    print("Fetching orderbook...")
    await client.fetch_orderbook_rest()
    print(f"  YES Best Bid: {client.yes_book.best_bid:.4f}  Ask: {client.yes_book.best_ask:.4f}")
    print(f"  NO  Best Bid: {client.no_book.best_bid:.4f}  Ask: {client.no_book.best_ask:.4f}")

    print("Fetching balance...")
    balance = await client.get_usdc_balance()
    print(f"  USDC Balance: ${balance:.2f}")

    tracker = PositionTracker(client, settings)
    tracker.update_cached_balance(balance)

    dashboard = Dashboard(client, tracker, settings)
    dashboard.update_balance(balance)
    dashboard.add_status("Bot initialized successfully")
    dashboard.add_status(f"Market: {market.question}")
    dashboard.add_status(f"Balance: ${balance:.2f}")

    # --- Phase 2: Register hotkeys ---
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    async def handle_buy_yes():
        dashboard.add_status("Executing BUY YES...")
        result = await tracker.buy_yes()
        if result.success:
            dashboard.add_status(f"YES order: {result.message}")
        else:
            dashboard.add_status(f"BUY YES failed: {result.message}")

    async def handle_buy_no():
        dashboard.add_status("Executing BUY NO...")
        result = await tracker.buy_no()
        if result.success:
            dashboard.add_status(f"NO order: {result.message}")
        else:
            dashboard.add_status(f"BUY NO failed: {result.message}")

    async def handle_emergency_close():
        dashboard.add_status("EMERGENCY CLOSE initiated!")
        result = await tracker.emergency_close(reason="MANUAL")
        dashboard.add_status(result.message)

    async def handle_quit():
        dashboard.add_status("Shutting down...")
        shutdown_event.set()

    def _dispatch(coro_func):
        """Bridge keyboard thread → asyncio loop."""
        asyncio.run_coroutine_threadsafe(coro_func(), loop)

    keyboard.on_press_key("z", lambda _: _dispatch(handle_buy_yes), suppress=False)
    keyboard.on_press_key("n", lambda _: _dispatch(handle_buy_no), suppress=False)
    keyboard.on_press_key("x", lambda _: _dispatch(handle_emergency_close), suppress=False)
    keyboard.on_press_key("q", lambda _: _dispatch(handle_quit), suppress=False)

    dashboard.add_status("Hotkeys active: [Z] YES  [N] NO  [X] Close  [Q] Quit")

    # --- Phase 3: Launch async tasks ---
    tasks = [
        asyncio.create_task(client.ws_connect(), name="ws_feed"),
        asyncio.create_task(tracker.stop_loss_monitor(), name="stop_loss"),
        asyncio.create_task(dashboard.run(), name="tui"),
        asyncio.create_task(dashboard.refresh_balance(), name="balance_poller"),
    ]

    # Wait for shutdown signal
    async def _wait_shutdown():
        await shutdown_event.wait()

    shutdown_task = asyncio.create_task(_wait_shutdown(), name="shutdown_waiter")
    tasks.append(shutdown_task)

    try:
        done, pending = await asyncio.wait(
            tasks,
            return_when=asyncio.FIRST_COMPLETED,
        )
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        # --- Phase 4: Graceful shutdown ---
        logger.info("Shutting down...")

        # Stop dashboard
        dashboard.stop()

        # Unhook keyboard
        try:
            keyboard.unhook_all()
        except Exception:
            pass

        # Cancel open orders for safety
        try:
            client.cancel_all_orders()
            logger.info("Cancelled all open orders")
        except Exception as e:
            logger.warning(f"Failed to cancel orders on shutdown: {e}")

        # Shutdown WebSocket
        client.shutdown()

        # Cancel remaining tasks
        for t in tasks:
            if not t.done():
                t.cancel()

        # Wait for cancellation
        await asyncio.gather(*tasks, return_exceptions=True)

        print("\nBot stopped. Goodbye!")


def main():
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        print("\nInterrupted. Exiting...")
    except Exception as e:
        print(f"\nFatal error: {e}")
        logger.exception("Fatal error")
        sys.exit(1)


if __name__ == "__main__":
    main()
