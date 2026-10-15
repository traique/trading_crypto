"""Coin identity for prompts and news matching: ticker, common name, a few aliases.

A small built-in table covers the large pairs; anything else falls back to the
ticker alone (CoinGecko's name is added by ``tokenomics`` when it is cached).
"""

from __future__ import annotations

import functools
import re

from cryptotrader.data.symbols import base_asset

# Ticker -> (common name, extra aliases). Aliases are matched case-insensitively.
COIN_NAMES: dict[str, tuple[str, tuple[str, ...]]] = {
    "BTC": ("Bitcoin", ()),
    "ETH": ("Ethereum", ("Ether",)),
    "BNB": ("BNB", ("Binance Coin", "BNB Chain")),
    "SOL": ("Solana", ()),
    "XRP": ("XRP", ("Ripple",)),
    "ADA": ("Cardano", ()),
    "DOGE": ("Dogecoin", ()),
    "TRX": ("TRON", ()),
    "TON": ("Toncoin", ("The Open Network",)),
    "AVAX": ("Avalanche", ()),
    "DOT": ("Polkadot", ()),
    "LINK": ("Chainlink", ()),
    "MATIC": ("Polygon", ()),
    "POL": ("Polygon", ()),
    "LTC": ("Litecoin", ()),
    "BCH": ("Bitcoin Cash", ()),
    "SHIB": ("Shiba Inu", ()),
    "UNI": ("Uniswap", ()),
    "ATOM": ("Cosmos", ()),
    "XLM": ("Stellar", ()),
    "NEAR": ("NEAR Protocol", ()),
    "APT": ("Aptos", ()),
    "ARB": ("Arbitrum", ()),
    "OP": ("Optimism", ()),
    "SUI": ("Sui", ()),
    "FIL": ("Filecoin", ()),
    "ETC": ("Ethereum Classic", ()),
    "ICP": ("Internet Computer", ()),
    "HBAR": ("Hedera", ()),
    "INJ": ("Injective", ()),
    "AAVE": ("Aave", ()),
    "PEPE": ("Pepe", ()),
    "WIF": ("dogwifhat", ()),
    "TIA": ("Celestia", ()),
    "SEI": ("Sei", ()),
    "RNDR": ("Render", ()),
    "RENDER": ("Render", ()),
    "FET": ("Fetch.ai", ("Artificial Superintelligence Alliance",)),
    "ENA": ("Ethena", ()),
    "TAO": ("Bittensor", ()),
}


# Other assets whose names contain a large coin's name: a headline about them is not
# news of that coin ("Bitcoin Cash surges" is not BTC news). Masked before matching,
# together with every name of the table that belongs to another ticker.
OTHER_ASSET_NAMES: tuple[str, ...] = (
    "Bitcoin Cash", "Bitcoin SV", "Bitcoin Gold", "Bitcoin Diamond", "Wrapped Bitcoin", "Wrapped BTC",
    "Bitcoin ETF Token", "Ethereum Classic", "Ethereum PoW", "EthereumPoW", "Ethereum Name Service",
    "Wrapped Ether", "Wrapped Ethereum", "Wrapped ETH", "Staked Ether", "Staked ETH", "Lido Staked Ether",
    "Rocket Pool ETH", "Coinbase Wrapped Staked ETH", "Wrapped Solana", "Wrapped SOL", "Wrapped BNB",
    "Polygon zkEVM", "Shiba Inu Treat", "Dogecoin Cash", "Litecoin Cash", "Cardano Classic",
)


@functools.lru_cache(maxsize=256)
def _other_names(ticker: str, own: tuple[str, ...]) -> tuple[str, ...]:
    """Longer names of OTHER assets that contain one of ``own`` (or the ticker) as a word."""
    own_lower = {n.lower() for n in own}
    candidates = set(OTHER_ASSET_NAMES)
    for other, (name, more) in COIN_NAMES.items():
        if other != ticker:
            candidates.update(n for n in (name, *more) if n)
    out = []
    for cand in candidates:
        if cand.lower() in own_lower:
            continue
        if any(re.search(rf"(?<![\w]){re.escape(n)}(?![\w])", cand, re.IGNORECASE) for n in [*own, ticker]):
            out.append(cand)
    return tuple(sorted(out, key=len, reverse=True))


def coin_name(symbol: str) -> str | None:
    entry = COIN_NAMES.get(base_asset(symbol))
    return entry[0] if entry else None


def aliases(symbol: str, extra_name: str | None = None) -> tuple[str, list[str]]:
    """(ticker, names) for matching headlines: the ticker case-sensitively, the names case-insensitively."""
    ticker = base_asset(symbol)
    name, more = COIN_NAMES.get(ticker, (None, ()))
    names = [n for n in (name, extra_name, *more) if n and n.upper() != ticker]
    return ticker, list(dict.fromkeys(names))


def mentions(title: str, symbol: str, extra_name: str | None = None) -> bool:
    """Whether a headline names the coin: its ticker as a word (upper case) or one of its names."""
    if not title:
        return False
    ticker, names = aliases(symbol, extra_name)
    for other in _other_names(ticker, tuple(names)):
        title = re.sub(rf"(?<![\w]){re.escape(other)}(?![\w])", " ", title, flags=re.IGNORECASE)
    if re.search(rf"(?<![A-Za-z0-9$]){re.escape(ticker)}(?![A-Za-z0-9])", title.replace("$" + ticker, ticker)):
        return True
    return any(re.search(rf"(?<![\w]){re.escape(n)}(?![\w])", title, re.IGNORECASE) for n in names)
