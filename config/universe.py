"""The research and trading universe.

Fixed in advance of results. Changing it is a new experiment: record the date
and the reason here, and re-run research.

2026-10-12 (v1): four index ETFs + eight largest megacaps.
2026-10-12 (v2): + 18 of the most liquid US stocks and sector ETFs, to raise the
    sample size after v1 left two families with too few trades. Chosen for
    liquidity and fractional-share eligibility, before any per-symbol result was
    examined (none has been).
"""

INDEX = "SPY"

UNIVERSE = [
    "SPY", "QQQ", "IWM", "DIA",
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD",
    "AVGO", "NFLX", "PLTR", "MU", "JPM", "ORCL", "CRM", "COIN", "UBER", "INTC",
    "BA", "XOM", "LLY", "UNH", "SMH", "XLF", "XLE", "XLK",
]
