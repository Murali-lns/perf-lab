"""Tiny moving-average backtest used to demo Perf Lab. Deterministic: fixed seed."""
import random

random.seed(42)
PRICES = [100.0]
for _ in range(300_000):
    PRICES.append(round(PRICES[-1] * (1 + random.uniform(-0.01, 0.0102)), 4))


def moving_average(prices, i, n):
    # Slow on purpose: re-adds the whole window for every day.
    window = prices[max(0, i - n + 1): i + 1]
    total = 0.0
    for p in window:
        total += p
    return total / len(window)


def calc_signals(prices, fast=20, slow=200):
    signals = []
    for i in range(len(prices)):
        f = moving_average(prices, i, fast)
        s = moving_average(prices, i, slow)
        signals.append(1 if f > s else 0)
    return signals


def run():
    signals = calc_signals(PRICES)
    cash, shares, trades = 100_000.0, 0.0, 0
    for price, sig in zip(PRICES, signals):
        if sig and shares == 0:
            shares, cash, trades = cash / price, 0.0, trades + 1
        elif not sig and shares:
            cash, shares, trades = shares * price, 0.0, trades + 1
    final = cash + shares * PRICES[-1]
    print(f"trades={trades}")
    print(f"final_value={final:.2f}")
    print(f"buy_days={sum(signals)}")


if __name__ == "__main__":
    run()
