"""Position tracking, trade execution, stop-loss monitoring, and PnL calculation."""

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from typing import Optional

from config import Settings
from polymarket import PolymarketClient

logger = logging.getLogger(__name__)

BUY = "BUY"
SELL = "SELL"


@dataclass
class Position:
    side: str  # "YES" or "NO"
    token_id: str
    size: float
    avg_entry_price: float
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    pnl_pct: float = 0.0
    order_id: str = ""
    stop_loss_triggered: bool = False


@dataclass
class TradeResult:
    success: bool
    side: str = ""
    price: float = 0.0
    size: float = 0.0
    message: str = ""


class PositionTracker:
    def __init__(self, client: PolymarketClient, settings: Settings):
        self._client = client
        self._settings = settings
        self._position: Optional[Position] = None
        self._lock = asyncio.Lock()
        self._stop_loss_active = asyncio.Event()
        self._cached_balance: float = 0.0
        self._trade_log: list[TradeResult] = []

    @property
    def position(self) -> Optional[Position]:
        return self._position

    @property
    def trade_log(self) -> list[TradeResult]:
        return self._trade_log[-20:]

    def update_cached_balance(self, balance: float) -> None:
        self._cached_balance = balance

    # ------------------------------------------------------------------
    # Buy YES
    # ------------------------------------------------------------------
    async def buy_yes(self) -> TradeResult:
        async with self._lock:
            if self._position and self._position.side == "NO":
                return TradeResult(False, message="Close NO position first (press X)")
            if self._position and self._position.side == "YES":
                return TradeResult(False, message="Already in YES position")

            market = self._client.market
            if not market:
                return TradeResult(False, message="No market discovered")

            book = self._client.yes_book
            if not book.asks:
                return TradeResult(False, message="No asks in YES orderbook")

            # Compute price: best ask + slippage offset
            price = book.best_ask + self._settings.max_slippage_pct
            tick = float(market.tick_size)
            price = max(tick, min(1.0 - tick, round(price / tick) * tick))

            # Compute size from balance
            balance = self._cached_balance
            if balance <= 0:
                return TradeResult(False, message="Insufficient USDC balance")

            amount_usdc = balance * self._settings.trade_amount_pct
            size = math.floor(amount_usdc / price)
            if size < 1:
                return TradeResult(False, message=f"Trade too small: ${amount_usdc:.2f} at {price}")

            result = self._client.create_and_post_limit_order(
                side=BUY,
                token_id=market.yes_token_id,
                price=price,
                size=float(size),
            )

            if result is None:
                tr = TradeResult(False, message="Order placement failed")
                self._trade_log.append(tr)
                return tr

            self._position = Position(
                side="YES",
                token_id=market.yes_token_id,
                size=float(size),
                avg_entry_price=price,
                current_price=book.best_bid,
                order_id=result.get("orderID", result.get("id", "")),
            )
            self._stop_loss_active.set()

            tr = TradeResult(True, side="YES", price=price, size=float(size),
                             message=f"BUY YES {size}@{price:.4f}")
            self._trade_log.append(tr)
            return tr

    # ------------------------------------------------------------------
    # Buy NO
    # ------------------------------------------------------------------
    async def buy_no(self) -> TradeResult:
        async with self._lock:
            if self._position and self._position.side == "YES":
                return TradeResult(False, message="Close YES position first (press X)")
            if self._position and self._position.side == "NO":
                return TradeResult(False, message="Already in NO position")

            market = self._client.market
            if not market:
                return TradeResult(False, message="No market discovered")

            book = self._client.no_book
            if not book.asks:
                return TradeResult(False, message="No asks in NO orderbook")

            price = book.best_ask + self._settings.max_slippage_pct
            tick = float(market.tick_size)
            price = max(tick, min(1.0 - tick, round(price / tick) * tick))

            balance = self._cached_balance
            if balance <= 0:
                return TradeResult(False, message="Insufficient USDC balance")

            amount_usdc = balance * self._settings.trade_amount_pct
            size = math.floor(amount_usdc / price)
            if size < 1:
                return TradeResult(False, message=f"Trade too small: ${amount_usdc:.2f} at {price}")

            result = self._client.create_and_post_limit_order(
                side=BUY,
                token_id=market.no_token_id,
                price=price,
                size=float(size),
            )

            if result is None:
                tr = TradeResult(False, message="Order placement failed")
                self._trade_log.append(tr)
                return tr

            self._position = Position(
                side="NO",
                token_id=market.no_token_id,
                size=float(size),
                avg_entry_price=price,
                current_price=book.best_bid,
                order_id=result.get("orderID", result.get("id", "")),
            )
            self._stop_loss_active.set()

            tr = TradeResult(True, side="NO", price=price, size=float(size),
                             message=f"BUY NO {size}@{price:.4f}")
            self._trade_log.append(tr)
            return tr

    # ------------------------------------------------------------------
    # Emergency Close
    # ------------------------------------------------------------------
    async def emergency_close(self, reason: str = "MANUAL") -> TradeResult:
        async with self._lock:
            if not self._position:
                return TradeResult(False, message="No position to close")

            pos = self._position
            market = self._client.market

            # Cancel any pending orders first
            self._client.cancel_all_orders()

            # Determine sell price - aggressive into the bids
            if pos.side == "YES":
                book = self._client.yes_book
            else:
                book = self._client.no_book

            if not book.bids:
                # Last resort: sell at minimum price
                sell_price = float(market.tick_size) if market else 0.01
            else:
                sell_price = book.best_bid - self._settings.max_slippage_pct

            tick = float(market.tick_size) if market else 0.01
            sell_price = max(tick, round(sell_price / tick) * tick)

            result = self._client.create_and_post_limit_order(
                side=SELL,
                token_id=pos.token_id,
                price=sell_price,
                size=pos.size,
            )

            # Clear position regardless of sell success
            pnl = (sell_price - pos.avg_entry_price) * pos.size
            old_pos = self._position
            self._position = None
            self._stop_loss_active.clear()

            if result is None:
                tr = TradeResult(False, message=f"{reason} CLOSE failed - position cleared")
                self._trade_log.append(tr)
                return tr

            tr = TradeResult(
                True, side=old_pos.side, price=sell_price, size=old_pos.size,
                message=f"{reason} CLOSE {old_pos.side} {old_pos.size}@{sell_price:.4f} PnL=${pnl:+.2f}"
            )
            self._trade_log.append(tr)
            return tr

    # ------------------------------------------------------------------
    # Stop-Loss Monitor
    # ------------------------------------------------------------------
    async def stop_loss_monitor(self) -> None:
        """Continuously monitor position and trigger stop-loss when threshold is hit."""
        while True:
            await self._stop_loss_active.wait()

            while self._position is not None:
                pos = self._position
                if pos is None:
                    break

                # Get current price from the relevant orderbook
                if pos.side == "YES":
                    book = self._client.yes_book
                else:
                    book = self._client.no_book

                # Skip if orderbook is stale
                if self._client.is_book_stale():
                    await asyncio.sleep(0.5)
                    continue

                current_price = book.best_bid if book.bids else pos.avg_entry_price

                # Update position state
                pos.current_price = current_price
                if pos.avg_entry_price > 0:
                    pos.pnl_pct = (current_price - pos.avg_entry_price) / pos.avg_entry_price
                    pos.unrealized_pnl = (current_price - pos.avg_entry_price) * pos.size

                # Check stop-loss
                if pos.pnl_pct <= -self._settings.stop_loss_pct:
                    logger.warning(
                        f"STOP LOSS triggered: PnL={pos.pnl_pct:.2%} <= -{self._settings.stop_loss_pct:.2%}"
                    )
                    pos.stop_loss_triggered = True
                    await self.emergency_close(reason="STOP-LOSS")
                    break

                await asyncio.sleep(0.25)  # 4Hz check rate
