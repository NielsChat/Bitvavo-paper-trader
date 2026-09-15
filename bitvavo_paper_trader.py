"""
Bitvavo Paper Trader v2
-----------------------
PAPER TRADING ONLY.
Geen API-key nodig.
Geen echte orders.
Startkapitaal: EUR 50

Strategie:
- Scant automatisch EUR-markten op Bitvavo.
- Gebruikt 1-uurs candles.
- Berekent EMA20 en EMA50.
- Koopt bij een bullish crossover + momentum.
- Verkoopt bij bearish crossover, stop-loss of take-profit.
- Maximaal enkele posities tegelijk.
- Slaat portefeuille en transacties lokaal op.
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests


# =========================
# INSTELLINGEN
# =========================

START_EUR = 50.00

CHECK_INTERVAL_SECONDS = 300       # iedere 5 minuten
CANDLE_INTERVAL = "1h"
CANDLE_LIMIT = 120

MAX_POSITIONS = 3

POSITION_SIZE_PERCENT = 0.30       # max 30% van beschikbare EUR per positie

STOP_LOSS_PERCENT = 0.05           # -5%
TAKE_PROFIT_PERCENT = 0.10         # +10%

FEE_PERCENT = 0.0025               # eenvoudige simulatie van 0,25%

MIN_VOLUME_EUR = 10000

API_BASE = "https://api.bitvavo.com/v2"

STATE_FILE = Path("paper_state.json")


# =========================
# HTTP
# =========================

session = requests.Session()
session.headers.update({
    "Accept": "application/json",
    "User-Agent": "Bitvavo-Paper-Trader/2.0"
})


def api_get(path, params=None):
    url = API_BASE + path

    response = session.get(
        url,
        params=params,
        timeout=20
    )

    response.raise_for_status()
    return response.json()


# =========================
# DATA
# =========================

def get_markets():
    markets = api_get("/markets")

    return [
        market
        for market in markets
        if (
            market.get("status") == "trading"
            and market.get("quote") == "EUR"
        )
    ]


def get_prices():
    data = api_get("/ticker/price")

    return {
        item["market"]: float(item["price"])
        for item in data
        if item.get("market")
    }


def get_24h_data(market):
    data = api_get(
        "/ticker/24h",
        {"market": market}
    )

    if isinstance(data, list) and data:
        return data[0]

    return None


def get_candles(market):
    data = api_get(
        f"/{market}/candles",
        {
            "interval": CANDLE_INTERVAL,
            "limit": CANDLE_LIMIT
        }
    )

    # Bitvavo retourneert candles van nieuw naar oud.
    data = list(reversed(data))

    return [
        {
            "timestamp": int(c[0]),
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4]),
            "volume": float(c[5])
        }
        for c in data
    ]


# =========================
# INDICATORS
# =========================

def ema(values, period):
    if len(values) < period:
        return None

    multiplier = 2 / (period + 1)

    result = sum(values[:period]) / period

    for value in values[period:]:
        result = (
            (value - result) * multiplier
            + result
        )

    return result


def calculate_signal(candles):
    if len(candles) < 60:
        return "HOLD", None

    closes = [c["close"] for c in candles]

    current_ema20 = ema(closes, 20)
    current_ema50 = ema(closes, 50)

    previous_closes = closes[:-1]

    previous_ema20 = ema(previous_closes, 20)
    previous_ema50 = ema(previous_closes, 50)

    current_price = closes[-1]

    if (
        current_ema20 is None
        or current_ema50 is None
        or previous_ema20 is None
        or previous_ema50 is None
    ):
        return "HOLD", None

    # Bullish crossover
    if (
        previous_ema20 <= previous_ema50
        and current_ema20 > current_ema50
        and current_price > current_ema20
    ):
        return "BUY", current_price

    # Bearish crossover
    if (
        previous_ema20 >= previous_ema50
        and current_ema20 < current_ema50
    ):
        return "SELL", current_price

    return "HOLD", current_price


# =========================
# STATE
# =========================

def default_state():
    return {
        "eur": START_EUR,
        "positions": {},
        "trades": [],
        "created": datetime.now(timezone.utc).isoformat()
    }


def load_state():
    if not STATE_FILE.exists():
        state = default_state()
        save_state(state)
        return state

    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        print("Kon state niet lezen. Nieuwe paper-portefeuille gestart.")
        return default_state()


def save_state(state):
    temp_file = STATE_FILE.with_suffix(".tmp")

    with open(temp_file, "w") as f:
        json.dump(
            state,
            f,
            indent=2
        )

    temp_file.replace(STATE_FILE)


# =========================
# PORTFOLIO
# =========================

def portfolio_value(state, prices):
    value = state["eur"]

    for market, position in state["positions"].items():
        price = prices.get(market)

        if price:
            value += position["amount"] * price

    return value


def print_portfolio(state, prices):
    total = portfolio_value(state, prices)

    print()
    print("=" * 60)
    print("PAPER PORTFOLIO")
    print("=" * 60)

    print(f"Cash:       €{state['eur']:.2f}")
    print(f"Totale waarde: €{total:.2f}")

    if START_EUR:
        pnl = total - START_EUR
        pnl_percent = (pnl / START_EUR) * 100

        print(
            f"P/L:        €{pnl:.2f} "
            f"({pnl_percent:+.2f}%)"
        )

    if state["positions"]:
        print()
        print("Posities:")

        for market, position in state["positions"].items():
            price = prices.get(market, position["entry"])

            current_value = position["amount"] * price
            invested = position["amount"] * position["entry"]

            pnl = current_value - invested

            print(
                f"{market}: "
                f"{position['amount']:.8f} "
                f"@ €{position['entry']:.4f} "
                f"| P/L €{pnl:+.2f}"
            )
    else:
        print("Geen open posities.")

    print("=" * 60)


# =========================
# TRADING
# =========================

def buy(state, market, price):
    if market in state["positions"]:
        return

    if len(state["positions"]) >= MAX_POSITIONS:
        return

    amount_eur = state["eur"] * POSITION_SIZE_PERCENT

    if amount_eur < 5:
        return

    fee = amount_eur * FEE_PERCENT
    total_cost = amount_eur + fee

    if total_cost > state["eur"]:
        return

    amount_asset = amount_eur / price

    state["eur"] -= total_cost

    state["positions"][market] = {
        "amount": amount_asset,
        "entry": price,
        "entry_time": datetime.now(
            timezone.utc
        ).isoformat()
    }

    trade = {
        "time": datetime.now(
            timezone.utc
        ).isoformat(),
        "market": market,
        "side": "BUY",
        "price": price,
        "amount": amount_asset,
        "value": amount_eur,
        "fee": fee
    }

    state["trades"].append(trade)

    print(
        f"🟢 PAPER BUY {market} "
        f"€{amount_eur:.2f} @ €{price:.4f}"
    )


def sell(state, market, price, reason):
    if market not in state["positions"]:
        return

    position = state["positions"][market]

    gross_value = position["amount"] * price

    fee = gross_value * FEE_PERCENT
    net_value = gross_value - fee

    invested = position["amount"] * position["entry"]

    pnl = net_value - invested

    state["eur"] += net_value

    trade = {
        "time": datetime.now(
            timezone.utc
        ).isoformat(),
        "market": market,
        "side": "SELL",
        "price": price,
        "amount": position["amount"],
        "value": gross_value,
        "fee": fee,
        "pnl": pnl,
        "reason": reason
    }

    state["trades"].append(trade)

    del state["positions"][market]

    print(
        f"🔴 PAPER SELL {market} "
        f"@ €{price:.4f} "
        f"| P/L €{pnl:+.2f} "
        f"| {reason}"
    )


def manage_positions(state, prices):
    for market in list(state["positions"].keys()):

        price = prices.get(market)

        if not price:
            continue

        position = state["positions"][market]

        entry = position["entry"]

        change = (price - entry) / entry

        if change <= -STOP_LOSS_PERCENT:
            sell(
                state,
                market,
                price,
                "STOP_LOSS"
            )

        elif change >= TAKE_PROFIT_PERCENT:
            sell(
                state,
                market,
                price,
                "TAKE_PROFIT"
            )


# =========================
# SCANNER
# =========================

def scan_market(market):
    try:
        candles = get_candles(market)

        if len(candles) < 60:
            return None

        ticker = get_24h_data(market)

        if not ticker:
            return None

        volume_quote = float(
            ticker.get("volumeQuote", 0)
        )

        if volume_quote < MIN_VOLUME_EUR:
            return None

        signal, price = calculate_signal(candles)

        return {
            "market": market,
            "signal": signal,
            "price": price,
            "volume": volume_quote
        }

    except Exception as error:
        print(
            f"⚠️ {market}: {error}"
        )

        return None


# =========================
# MAIN LOOP
# =========================

def run_once(state):
    print()
    print(
        datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    )

    print("Markten ophalen...")

    markets = get_markets()

    print(
        f"{len(markets)} EUR-markten gevonden."
    )

    prices = get_prices()

    # Eerst bestaande posities beheren.
    manage_positions(
        state,
        prices
    )

    # Daarna nieuwe kansen zoeken.
    candidates = []

    for market_info in markets:

        market = market_info["market"]

        result = scan_market(market)

        if result:
            candidates.append(result)

        # Niet te agressief tegen API rate limits.
        time.sleep(0.05)

    buys = [
        candidate
        for candidate in candidates
        if candidate["signal"] == "BUY"
    ]

    # Hoogste 24h volume eerst.
    buys.sort(
        key=lambda x: x["volume"],
        reverse=True
    )

    for candidate in buys:

        if len(state["positions"]) >= MAX_POSITIONS:
            break

        buy(
            state,
            candidate["market"],
            candidate["price"]
        )

    save_state(state)

    print_portfolio(
        state,
        prices
    )

    print()
    print(
        f"Scans: {len(candidates)}"
        f" | Koopkandidaten: {len(buys)}"
    )


def main():
    print("=" * 60)
    print("BITVAVO PAPER TRADER v2")
    print("=" * 60)
    print("⚠️ PAPER TRADING ONLY")
    print(f"Startkapitaal: €{START_EUR:.2f}")
    print(
        f"Interval: {CHECK_INTERVAL_SECONDS} seconden"
    )
    print("=" * 60)

    state = load_state()

    while True:

        try:
            run_once(state)

        except KeyboardInterrupt:
            print()
            print("Bot gestopt.")
            break

        except Exception as error:
            print()
            print(
                f"⚠️ Algemene fout: {error}"
            )

        print()
        print(
            f"Volgende scan over "
            f"{CHECK_INTERVAL_SECONDS} seconden..."
        )

        time.sleep(
            CHECK_INTERVAL_SECONDS
        )


if __name__ == "__main__":
    main()
