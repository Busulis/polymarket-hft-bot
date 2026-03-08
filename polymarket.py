"""Polymarket CLOB client wrapper — market discovery, orderbook, orders, WebSocket."""

import asyncio
import json
import logging
import math
import random
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx
import websockets

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    BalanceAllowanceParams,
    OrderArgs,
    OrderType,
    PartialCreateOrderOptions,
)
from py_clob_client.constants import POLYGON

from config import (
    CHAIN_ID,
    CLOB_HOST,
    GAMMA_API,
    MARKET_ALIASES,
    STALE_ORDERBOOK_THRESHOLD,
    WS_PING_INTERVAL,
    WS_URL,
    Settings,
)

logger = logging.getLogger(__name__)


@dataclass
class MarketInfo:
    condition_id: str
    question: str
    slug: str
    yes_token_id: str
    no_token_id: str
    neg_risk: bool
    tick_size: str
    end_date: str
    active: bool


@dataclass
class OrderBookState:
    bids: list = field(default_factory=list)  # [(price, size), ...] desc by price
    asks: list = field(default_factory=list)  # [(price, size), ...] asc by price
    best_bid: float = 0.0
    best_ask: float = 1.0
    midpoint: float = 0.5
    spread: float = 1.0
    last_update: float = 0.0


class PolymarketClient:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._clob = ClobClient(
            host=CLOB_HOST,
            key=settings.private_key,
            chain_id=CHAIN_ID,
        )
        self._clob.set_api_creds(
            ApiCreds(
                api_key=settings.api_key,
                api_secret=settings.api_secret,
                api_passphrase=settings.api_passphrase,
            )
        )
        self.market: Optional[MarketInfo] = None
        self.yes_book = OrderBookState()
        self.no_book = OrderBookState()
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self.ws_connected = asyncio.Event()
        self._shutdown = asyncio.Event()

    # ------------------------------------------------------------------
    # Market Discovery
    # ------------------------------------------------------------------
    async def discover_market(self) -> MarketInfo:
        """Find the active BTC 5m/15m market via Gamma API."""
        alias = self._settings.default_market.upper()
        search_query = MARKET_ALIASES.get(alias, self._settings.default_market)

        async with httpx.AsyncClient(timeout=15) as http:
            resp = await http.get(
                f"{GAMMA_API}/markets",
                params={"closed": "false", "limit": "100", "search": search_query},
            )
            resp.raise_for_status()
            markets = resp.json()

        if not markets:
            raise RuntimeError(
                f"No active markets found for query: {search_query!r}"
            )

        # Filter active, sort by end_date (soonest first)
        active = [m for m in markets if m.get("active", False) and not m.get("closed", True)]
        if not active:
            raise RuntimeError("No active open markets found")

        active.sort(key=lambda m: m.get("endDate", ""))
        chosen = active[0]

        # Parse token IDs from clobTokenIds
        clob_tokens = chosen.get("clobTokenIds", [])
        if isinstance(clob_tokens, str):
            clob_tokens = json.loads(clob_tokens)

        if len(clob_tokens) < 2:
            raise RuntimeError(f"Market has <2 token IDs: {clob_tokens}")

        self.market = MarketInfo(
            condition_id=chosen["conditionId"],
            question=chosen.get("question", "Unknown"),
            slug=chosen.get("slug", ""),
            yes_token_id=clob_tokens[0],
            no_token_id=clob_tokens[1],
            neg_risk=chosen.get("negRisk", False),
            tick_size=str(chosen.get("minimum_tick_size", "0.01")),
            end_date=chosen.get("endDate", ""),
            active=True,
        )
        return self.market

    # ------------------------------------------------------------------
    # REST Order Book (initial snapshot)
    # ------------------------------------------------------------------
    async def fetch_orderbook_rest(self) -> None:
        """Fetch orderbook via REST for initial state."""
        if not self.market:
            raise RuntimeError("Market not discovered yet")

        loop = asyncio.get_running_loop()

        # Fetch YES book
        yes_raw = await loop.run_in_executor(
            None, self._clob.get_order_book, self.market.yes_token_id
        )
        self.yes_book = self._parse_book(yes_raw)

        # Fetch NO book
        no_raw = await loop.run_in_executor(
            None, self._clob.get_order_book, self.market.no_token_id
        )
        self.no_book = self._parse_book(no_raw)

    @staticmethod
    def _parse_book(raw) -> OrderBookState:
        """Parse SDK order book response into OrderBookState."""
        bids = []
        asks = []

        raw_bids = raw.bids if hasattr(raw, "bids") else raw.get("bids", [])
        raw_asks = raw.asks if hasattr(raw, "asks") else raw.get("asks", [])

        for b in raw_bids:
            price = float(b.price if hasattr(b, "price") else b.get("price", 0))
            size = float(b.size if hasattr(b, "size") else b.get("size", 0))
            bids.append((price, size))
        for a in raw_asks:
            price = float(a.price if hasattr(a, "price") else a.get("price", 0))
            size = float(a.size if hasattr(a, "size") else a.get("size", 0))
            asks.append((price, size))

        bids.sort(key=lambda x: x[0], reverse=True)
        asks.sort(key=lambda x: x[0])

        best_bid = bids[0][0] if bids else 0.0
        best_ask = asks[0][0] if asks else 1.0
        midpoint = (best_bid + best_ask) / 2 if bids and asks else 0.5
        spread = best_ask - best_bid if bids and asks else 1.0

        return OrderBookState(
            bids=bids,
            asks=asks,
            best_bid=best_bid,
            best_ask=best_ask,
            midpoint=midpoint,
            spread=spread,
            last_update=time.monotonic(),
        )

    # ------------------------------------------------------------------
    # Balance
    # ------------------------------------------------------------------
    async def get_usdc_balance(self) -> float:
        """Get USDC balance."""
        loop = asyncio.get_running_loop()
        try:
            bal = await loop.run_in_executor(
                None,
                lambda: self._clob.get_balance_allowance(
                    BalanceAllowanceParams(asset_type="COLLATERAL")
                ),
            )
            # bal may have different structures; try common patterns
            if isinstance(bal, dict):
                return float(bal.get("balance", 0))
            if hasattr(bal, "balance"):
                return float(bal.balance)
            return 0.0
        except Exception as e:
            logger.warning(f"Balance fetch failed: {e}")
            return 0.0

    # ------------------------------------------------------------------
    # Order Placement
    # ------------------------------------------------------------------
    def create_and_post_limit_order(
        self, side: str, token_id: str, price: float, size: float
    ) -> dict | None:
        """Create, sign, and post a limit order. Returns response or None."""
        if not self.market:
            return None

        tick = float(self.market.tick_size)
        price = max(tick, min(1.0 - tick, round(price / tick) * tick))
        price = round(price, len(self.market.tick_size.split(".")[-1]))

        try:
            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=side,
            )
            signed = self._clob.create_order(
                order_args,
                options=PartialCreateOrderOptions(
                    tick_size=self.market.tick_size,
                    neg_risk=self.market.neg_risk,
                ),
            )

            if self._settings.dry_run:
                logger.info(f"[DRY RUN] Order signed: {side} {size}@{price} token={token_id[:16]}...")
                return {"dry_run": True, "side": side, "price": price, "size": size}

            resp = self._clob.post_order(signed, OrderType.GTC)
            logger.info(f"Order posted: {resp}")
            return resp if isinstance(resp, dict) else {"response": str(resp)}

        except Exception as e:
            logger.error(f"Order failed: {e}")
            return None

    def cancel_all_orders(self) -> bool:
        """Cancel all open orders."""
        try:
            self._clob.cancel_all()
            return True
        except Exception as e:
            logger.warning(f"Cancel all failed: {e}")
            return False

    # ------------------------------------------------------------------
    # WebSocket
    # ------------------------------------------------------------------
    async def ws_connect(self) -> None:
        """Main WebSocket loop with auto-reconnect."""
        if not self.market:
            raise RuntimeError("Market not discovered")

        backoff = 1.0
        while not self._shutdown.is_set():
            try:
                async with websockets.connect(
                    WS_URL,
                    ping_interval=None,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    # Subscribe to both YES and NO token books
                    sub_msg = json.dumps({
                        "assets_ids": [
                            self.market.yes_token_id,
                            self.market.no_token_id,
                        ],
                        "type": "market",
                    })
                    await ws.send(sub_msg)
                    self.ws_connected.set()
                    backoff = 1.0
                    logger.info("WebSocket connected and subscribed")

                    reader = asyncio.create_task(self._ws_reader(ws))
                    pinger = asyncio.create_task(self._ws_pinger(ws))

                    done, pending = await asyncio.wait(
                        [reader, pinger],
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for t in pending:
                        t.cancel()
                    for t in done:
                        if t.exception():
                            logger.warning(f"WS task error: {t.exception()}")

            except (websockets.ConnectionClosed, OSError, Exception) as e:
                logger.warning(f"WebSocket disconnected: {e}")

            self.ws_connected.clear()
            self._ws = None

            if self._shutdown.is_set():
                break

            jitter = random.uniform(0, 0.5)
            await asyncio.sleep(backoff + jitter)
            backoff = min(backoff * 2, 30.0)

    async def _ws_reader(self, ws) -> None:
        """Read and parse WebSocket messages."""
        async for raw_msg in ws:
            try:
                data = json.loads(raw_msg)
                events = data if isinstance(data, list) else [data]
                for event in events:
                    self._process_ws_event(event)
            except json.JSONDecodeError:
                continue
            except Exception as e:
                logger.debug(f"WS event processing error: {e}")

    def _process_ws_event(self, event: dict) -> None:
        """Process a single WebSocket event and update the relevant orderbook."""
        if not self.market:
            return

        asset_id = event.get("asset_id", "")
        etype = event.get("event_type", "")

        if etype == "book":
            book = self._parse_ws_book(event)
            if asset_id == self.market.yes_token_id:
                self.yes_book = book
            elif asset_id == self.market.no_token_id:
                self.no_book = book

        elif etype == "price_change":
            changes = event.get("price_changes", [])
            if asset_id == self.market.yes_token_id:
                self._apply_price_changes(self.yes_book, changes)
            elif asset_id == self.market.no_token_id:
                self._apply_price_changes(self.no_book, changes)

        elif etype == "tick_size_change":
            new_tick = event.get("tick_size")
            if new_tick:
                self.market.tick_size = str(new_tick)

    @staticmethod
    def _parse_ws_book(event: dict) -> OrderBookState:
        """Parse a full book snapshot from WebSocket."""
        bids = []
        asks = []
        for b in event.get("bids", []):
            bids.append((float(b.get("price", 0)), float(b.get("size", 0))))
        for a in event.get("asks", []):
            asks.append((float(a.get("price", 0)), float(a.get("size", 0))))

        bids.sort(key=lambda x: x[0], reverse=True)
        asks.sort(key=lambda x: x[0])

        best_bid = bids[0][0] if bids else 0.0
        best_ask = asks[0][0] if asks else 1.0

        return OrderBookState(
            bids=bids,
            asks=asks,
            best_bid=best_bid,
            best_ask=best_ask,
            midpoint=(best_bid + best_ask) / 2,
            spread=best_ask - best_bid,
            last_update=time.monotonic(),
        )

    @staticmethod
    def _apply_price_changes(book: OrderBookState, changes: list) -> None:
        """Apply incremental price change updates."""
        for ch in changes:
            side = ch.get("side", "")
            price = float(ch.get("price", 0))
            size = float(ch.get("size", 0))

            if side == "BUY":
                # Update bids
                book.bids = [(p, s) for p, s in book.bids if p != price]
                if size > 0:
                    book.bids.append((price, size))
                book.bids.sort(key=lambda x: x[0], reverse=True)
                book.best_bid = book.bids[0][0] if book.bids else 0.0
            elif side == "SELL":
                # Update asks
                book.asks = [(p, s) for p, s in book.asks if p != price]
                if size > 0:
                    book.asks.append((price, size))
                book.asks.sort(key=lambda x: x[0])
                book.best_ask = book.asks[0][0] if book.asks else 1.0

        if book.bids and book.asks:
            book.midpoint = (book.best_bid + book.best_ask) / 2
            book.spread = book.best_ask - book.best_bid
        book.last_update = time.monotonic()

    async def _ws_pinger(self, ws) -> None:
        """Send periodic PING to keep WebSocket alive."""
        while True:
            await asyncio.sleep(WS_PING_INTERVAL)
            try:
                await ws.send("PING")
            except Exception:
                return  # Let the outer loop handle reconnect

    def is_book_stale(self) -> bool:
        """Check if orderbook data is stale."""
        if self.yes_book.last_update == 0:
            return True
        return (time.monotonic() - self.yes_book.last_update) > STALE_ORDERBOOK_THRESHOLD

    def shutdown(self) -> None:
        """Signal shutdown for WebSocket loop."""
        self._shutdown.set()
