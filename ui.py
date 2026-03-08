"""Rich TUI dashboard for the Polymarket trading bot."""

import asyncio
import time
from collections import deque
from datetime import datetime

from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from config import Settings, UI_REFRESH_HZ
from polymarket import PolymarketClient
from trading import PositionTracker


class Dashboard:
    def __init__(
        self,
        client: PolymarketClient,
        tracker: PositionTracker,
        settings: Settings,
    ):
        self._client = client
        self._tracker = tracker
        self._settings = settings
        self._status_log: deque[str] = deque(maxlen=15)
        self._usdc_balance: float = 0.0
        self._live: Live | None = None
        self._running = True

    def add_status(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self._status_log.append(f"[{ts}] {msg}")

    def update_balance(self, balance: float) -> None:
        self._usdc_balance = balance

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    # Build Panels
    # ------------------------------------------------------------------
    def _build_header(self) -> Panel:
        market = self._client.market
        question = market.question if market else "Discovering market..."

        t = Table.grid(padding=(0, 2))
        t.add_column(justify="left", min_width=40)
        t.add_column(justify="right", min_width=30)

        # Row 1: Market question
        t.add_row(
            Text(question, style="bold cyan"),
            Text(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), style="dim"),
        )

        # Row 2: Balance, size, SL
        balance_txt = Text(f"Balance: ${self._usdc_balance:.2f}", style="bold white")
        size_txt = Text(
            f"Size: {self._settings.trade_amount_pct:.0%}  "
            f"SL: {self._settings.stop_loss_pct:.0%}  "
            f"Slip: {self._settings.max_slippage_pct}",
            style="dim white",
        )
        t.add_row(balance_txt, size_txt)

        # Dry run warning
        if self._settings.dry_run:
            t.add_row(
                Text("DRY RUN MODE - Orders will NOT be sent", style="bold yellow"),
                Text("", style=""),
            )

        # WS status
        ws_status = "CONNECTED" if self._client.ws_connected.is_set() else "DISCONNECTED"
        ws_style = "green" if self._client.ws_connected.is_set() else "red"
        stale = " (STALE)" if self._client.is_book_stale() else ""
        t.add_row(
            Text(f"WebSocket: {ws_status}{stale}", style=ws_style),
            Text("", style=""),
        )

        return Panel(t, title="Polymarket BTC Bot", border_style="blue")

    def _build_orderbook(self) -> Panel:
        layout = Layout()
        layout.split_row(
            Layout(self._build_single_book("YES", self._client.yes_book), name="yes"),
            Layout(self._build_single_book("NO", self._client.no_book), name="no"),
        )
        return Panel(layout, title="Order Book", border_style="blue")

    def _build_single_book(self, label: str, book) -> Panel:
        table = Table(
            title=f"{label} Token",
            title_style="bold",
            show_header=True,
            header_style="bold",
            padding=(0, 1),
            expand=True,
        )
        table.add_column("Price", justify="right", style="bold")
        table.add_column("Size", justify="right")
        table.add_column("Total", justify="right", style="dim")

        # Top 3 asks (reversed so lowest ask is at bottom, closest to spread)
        asks = book.asks[:3]
        for price, size in reversed(asks):
            table.add_row(
                f"{price:.4f}",
                f"{size:.1f}",
                f"${price * size:.2f}",
                style="red",
            )

        # Spread line
        table.add_row(
            f"--- Spread: {book.spread:.4f} ---",
            "",
            "",
            style="dim yellow",
        )

        # Top 3 bids
        bids = book.bids[:3]
        for price, size in bids:
            table.add_row(
                f"{price:.4f}",
                f"{size:.1f}",
                f"${price * size:.2f}",
                style="green",
            )

        return Panel(table, border_style="dim")

    def _build_position(self) -> Panel:
        pos = self._tracker.position

        if pos is None:
            content = Text.assemble(
                ("No active position\n\n", "dim"),
                ("[Z]", "bold green"), " Buy YES   ",
                ("[N]", "bold red"), " Buy NO   ",
                ("[X]", "bold yellow"), " Close   ",
                ("[Q]", "bold magenta"), " Quit",
            )
            return Panel(content, title="Position", border_style="blue")

        table = Table(show_header=False, padding=(0, 2), expand=True)
        table.add_column("Label", style="dim", min_width=15)
        table.add_column("Value", min_width=20)

        side_style = "bold green" if pos.side == "YES" else "bold red"
        table.add_row("Side", Text(pos.side, style=side_style))
        table.add_row("Size", f"{pos.size:.0f} shares")
        table.add_row("Avg Entry", f"${pos.avg_entry_price:.4f}")
        table.add_row("Current", f"${pos.current_price:.4f}")

        # PnL styling
        pnl_color = "green" if pos.unrealized_pnl >= 0 else "red"
        pnl_style = f"bold {pnl_color}"

        # Flash near stop-loss
        if pos.pnl_pct <= -self._settings.stop_loss_pct * 0.8 and pos.pnl_pct < 0:
            pnl_style = "bold red blink"

        table.add_row(
            "Unrealized PnL",
            Text(f"${pos.unrealized_pnl:+.4f}", style=pnl_style),
        )
        table.add_row(
            "PnL %",
            Text(f"{pos.pnl_pct:+.2%}", style=pnl_style),
        )
        table.add_row(
            "Stop-Loss",
            Text(f"-{self._settings.stop_loss_pct:.0%}", style="dim yellow"),
        )

        if pos.stop_loss_triggered:
            table.add_row("", Text("STOP-LOSS TRIGGERED!", style="bold red blink"))

        return Panel(table, title="Position", border_style="blue")

    def _build_status(self) -> Panel:
        lines = list(self._status_log)
        if not lines:
            lines = ["Waiting for activity..."]

        text = Text()
        for line in lines[-8:]:  # Show last 8 messages
            if "STOP-LOSS" in line or "ERROR" in line or "failed" in line.lower():
                text.append(line + "\n", style="red")
            elif "BUY" in line or "posted" in line.lower() or "success" in line.lower():
                text.append(line + "\n", style="green")
            elif "DRY RUN" in line:
                text.append(line + "\n", style="yellow")
            elif "CLOSE" in line:
                text.append(line + "\n", style="yellow")
            else:
                text.append(line + "\n", style="dim")

        return Panel(text, title="Activity Log", border_style="blue")

    # ------------------------------------------------------------------
    # Render
    # ------------------------------------------------------------------
    def _render(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(self._build_header(), name="header", size=7 if self._settings.dry_run else 6),
            Layout(name="middle", ratio=2),
            Layout(self._build_status(), name="status", size=12),
        )
        layout["middle"].split_row(
            Layout(self._build_orderbook(), name="book", ratio=3),
            Layout(self._build_position(), name="position", ratio=2),
        )
        return layout

    # ------------------------------------------------------------------
    # Async Run
    # ------------------------------------------------------------------
    async def run(self) -> None:
        """Main TUI display loop."""
        with Live(
            self._render(),
            refresh_per_second=UI_REFRESH_HZ,
            screen=True,
        ) as live:
            self._live = live
            while self._running:
                try:
                    live.update(self._render())
                except Exception:
                    pass  # Don't crash the bot on render errors
                await asyncio.sleep(1 / UI_REFRESH_HZ)

    async def refresh_balance(self) -> None:
        """Periodically fetch USDC balance."""
        from config import BALANCE_POLL_INTERVAL

        while self._running:
            try:
                bal = await self._client.get_usdc_balance()
                self._usdc_balance = bal
                self._tracker.update_cached_balance(bal)
            except Exception:
                pass
            await asyncio.sleep(BALANCE_POLL_INTERVAL)
