# ⚡ Poly Trade — Ultra-Low Latency Polymarket Terminal

A high-speed CLI execution bot purpose-built for Polymarket's BTC 5-minute and 15-minute prediction markets. Keypress-to-wire in under 250ms. No GUI bloat, no browser tabs — just a terminal, hotkeys, and the orderbook.

<p align="center">
  <img src="https://img.shields.io/badge/python-3.9+-blue?logo=python&logoColor=white" />
  <img src="https://img.shields.io/badge/network-Polygon-8247E5?logo=polygon&logoColor=white" />
  <img src="https://img.shields.io/badge/protocol-CLOB-green" />
  <img src="https://img.shields.io/badge/mode-DRY%20RUN%20%7C%20LIVE-orange" />
</p>

---

## 🎯 Features

- **⌨️ Hotkey Execution** — One-key trades via `Z`, `N`, `X`. No confirmation dialogs. No mouse.
- **📊 Real-Time TUI** — Full-screen Rich dashboard with live YES/NO orderbooks, position tracking, and PnL.
- **🛡️ Automated Stop-Loss** — Monitors your position at 4 Hz and auto-exits when the loss threshold is hit.
- **🔌 WebSocket Orderbook Feed** — Live bid/ask data streamed from Polymarket's CLOB with auto-reconnect.
- **🧪 Dry Run Mode** — Sign and simulate orders locally without ever touching the blockchain.
- **⚙️ Smart Limit Orders** — Places limit orders at top-of-book ± slippage offset to avoid front-running.
- **🔄 Auto-Reconnect** — Exponential backoff with jitter on WebSocket drops. Stale-data detection at 30 seconds.

---

## 📋 Prerequisites

| Requirement | Details |
|:--|:--|
| **Python** | 3.9 or higher |
| **OS** | Windows (recommended), Linux/macOS (requires `sudo` for keyboard hooks) |
| **Wallet** | A Polygon wallet with USDC.e balance |
| **API Keys** | Polymarket CLOB credentials (`API_KEY`, `API_SECRET`, `PASSPHRASE`) |

> **How to get Polymarket API keys:**
> Use the `py-clob-client` SDK's `create_or_derive_api_creds()` method with your wallet private key, or follow the [Polymarket CLOB Authentication Guide](https://docs.polymarket.com/developers/CLOB/authentication).

---

## 🚀 Installation

```bash
# Clone the repository
git clone https://github.com/<your-username>/poly_trade.git
cd poly_trade

# Install dependencies
pip install -r requirements.txt

# Create your environment file
cp .env.template .env
```

---

## 🔧 Configuration

Copy `.env.template` to `.env` and fill in every field:

```env
# Wallet & Auth
POLYGON_PRIVATE_KEY=your_64_char_hex_key
POLY_API_KEY=your-api-key
POLY_API_SECRET=your-api-secret
POLY_API_PASS=your-passphrase

# Market
DEFAULT_MARKET=BTC-5          # BTC-5 or BTC-15

# Trade Parameters
TRADE_AMOUNT_PERCENT=0.10     # 10% of USDC balance per trade
STOP_LOSS_PERCENT=0.05        # 5% loss triggers auto-exit
MAX_SLIPPAGE_PERCENT=0.02     # 2-cent limit order offset

# Safety
DRY_RUN=true                  # Start in dry run — set to false for live
```

| Variable | Type | Range | Description |
|:--|:--|:--|:--|
| `POLYGON_PRIVATE_KEY` | hex string | 64 chars | Your Polygon wallet private key |
| `POLY_API_KEY` | string | — | CLOB API key |
| `POLY_API_SECRET` | string | — | CLOB API secret |
| `POLY_API_PASS` | string | — | CLOB API passphrase |
| `DEFAULT_MARKET` | string | `BTC-5` / `BTC-15` | Target market timeframe |
| `TRADE_AMOUNT_PERCENT` | float | 0.001 – 1.0 | Fraction of balance per trade |
| `STOP_LOSS_PERCENT` | float | 0.001 – 0.99 | Auto-exit loss threshold |
| `MAX_SLIPPAGE_PERCENT` | float | 0.0 – 0.50 | Limit price offset from best price |
| `DRY_RUN` | bool | `true` / `false` | Simulate orders without posting |

---

## 🖥️ Usage

### Start the bot

```bash
# Windows — run as Administrator for keyboard hooks
python bot.py

# Linux / macOS — requires sudo
sudo python bot.py
```

> **⚠️ Windows users:** The `keyboard` library requires Administrator privileges to register global hotkeys. Right-click your terminal and select **Run as Administrator** before launching.

### Hotkeys

Once the TUI is running, all trading is done through single keypresses:

| Key | Action | Behavior |
|:---:|:--|:--|
| `Z` | **Buy YES** | Places a limit buy on the YES token at best ask + slippage |
| `N` | **Buy NO** | Places a limit buy on the NO token at best ask + slippage |
| `X` | **Emergency Close** | Cancels all open orders, sells position at best bid − slippage |
| `Q` | **Quit** | Cancels all open orders and exits gracefully |

### Dashboard Layout

```
┌──────────────────────────────────────────────────┐
│  Market: "Will BTC go up in the next 5 minutes?" │
│  Balance: $142.50  │  Size: 10%  │  SL: 5%      │
│  WebSocket: CONNECTED                            │
├────────────────────────┬─────────────────────────┤
│      ORDER BOOK        │       POSITION          │
│  YES         NO        │  Side: YES              │
│  Ask  0.55   Ask 0.47  │  Entry: $0.5300         │
│  ---spread---          │  Current: $0.5500       │
│  Bid  0.53   Bid 0.45  │  PnL: +$4.00 (+3.77%)  │
├────────────────────────┴─────────────────────────┤
│  [12:01:05] BUY YES 20@0.5300                    │
│  [12:01:10] Position updated                     │
│  [Z] YES  [N] NO  [X] Close  [Q] Quit           │
└──────────────────────────────────────────────────┘
```

### Recommended First Run

1. Set `DRY_RUN=true` in `.env`
2. Launch `python bot.py`
3. Verify market discovery and orderbook display
4. Press `Z` or `N` — confirm simulated order appears in the activity log
5. Press `X` — confirm simulated close
6. When confident, set `DRY_RUN=false` and `TRADE_AMOUNT_PERCENT` to a small value (e.g., `0.02`)

---

## 🏗️ Architecture

```
bot.py                  ← Entry point, asyncio loop, keyboard bridge
  ├── config.py         ← .env loader, Settings dataclass, constants
  ├── polymarket.py     ← CLOB client, market discovery, WS feed, orders
  ├── trading.py        ← Position tracker, stop-loss monitor, PnL engine
  └── ui.py             ← Rich TUI dashboard (Live display)
```

**Concurrency model:** A single `asyncio` event loop runs four concurrent tasks — WebSocket feed, stop-loss monitor, TUI renderer, and balance poller. The `keyboard` library hooks into the OS in a background thread and dispatches into the async loop via `asyncio.run_coroutine_threadsafe()`.

**Latency budget:**

| Stage | Time |
|:--|:--|
| Keypress detection | < 1 ms |
| Orderbook read (in-memory) | < 0.01 ms |
| EIP-712 order signing | ~ 1 ms |
| HTTP POST to CLOB | 50 – 200 ms |
| **Total keypress-to-wire** | **~ 50 – 250 ms** |

---

## 🛠️ Tech Stack

| Component | Library | Purpose |
|:--|:--|:--|
| Trading Protocol | `py-clob-client` | Polymarket CLOB SDK — order signing, posting, book queries |
| Async Runtime | `asyncio` | Concurrent WebSocket, TUI, and monitoring tasks |
| WebSocket | `websockets` | Real-time orderbook feed from Polymarket |
| HTTP Client | `httpx` | Async market discovery via Gamma API |
| Terminal UI | `rich` | Full-screen Live dashboard with tables and color |
| Hotkeys | `keyboard` | OS-level global keypress detection |
| Config | `python-dotenv` | Loads `.env` into `os.environ` |

---

## ⚠️ Disclaimer

> **This software is provided as-is for educational and experimental purposes only.**
>
> - This is a **high-risk trading tool** that executes real financial transactions on the Polygon blockchain when `DRY_RUN=false`.
> - Prediction markets are speculative and volatile. **You can lose your entire balance.**
> - The author(s) assume **no responsibility** for any financial losses, missed trades, bugs, API outages, or any other damages resulting from the use of this software.
> - **Never trade with funds you cannot afford to lose.**
> - You are solely responsible for securing your private keys and API credentials.
> - This project is not affiliated with, endorsed by, or associated with Polymarket in any way.
>
> **By using this software, you accept all risks.**

---

## 📄 License

MIT

---

<p align="center">
  Built for speed. No compromises.
</p>
