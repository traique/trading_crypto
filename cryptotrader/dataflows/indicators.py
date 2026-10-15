"""Technical indicators in plain pandas, on the closed daily candles of ``cryptotrader.data.load_ohlcv``.

Every indicator at row i uses rows <= i only (no centred windows, no
back-fill), so a value is what was computable at that candle's close. Names
follow the usual stockstats spelling the prompts use.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

import numpy as np
import pandas as pd

from cryptotrader.data.errors import NoMarketDataError, VendorError
from cryptotrader.data.symbols import normalize_symbol
from cryptotrader.dataflows import fmt


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False, min_periods=span).mean()


def _wilder(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def sma(close: pd.Series, n: int) -> pd.Series:
    return close.rolling(n, min_periods=n).mean()


def ema(close: pd.Series, n: int) -> pd.Series:
    return _ema(close, n)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = _ema(close, fast) - _ema(close, slow)
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame({"macd": line, "macds": sig, "macdh": line - sig})


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = _wilder(delta.clip(lower=0.0), period)
    loss = _wilder((-delta).clip(lower=0.0), period)
    rs = gain / loss.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    # No loss at all over the window: RSI is 100 (when there was any gain).
    out = out.where(~((loss == 0) & (gain > 0)), 100.0)
    return out


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.DataFrame:
    mid = sma(close, n)
    std = close.rolling(n, min_periods=n).std(ddof=0)
    return pd.DataFrame({"boll": mid, "boll_ub": mid + k * std, "boll_lb": mid - k * std})


def true_range(frame: pd.DataFrame) -> pd.Series:
    prev = frame["Close"].shift(1)
    ranges = pd.concat([frame["High"] - frame["Low"], (frame["High"] - prev).abs(),
                        (frame["Low"] - prev).abs()], axis=1)
    return ranges.max(axis=1, skipna=True)


def atr(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    return _wilder(true_range(frame), period)


def vwma(frame: pd.DataFrame, n: int = 14) -> pd.Series:
    pv = (frame["Close"] * frame["Volume"]).rolling(n, min_periods=n).sum()
    vol = frame["Volume"].rolling(n, min_periods=n).sum()
    return pv / vol.replace(0.0, np.nan)


def mfi(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    typical = (frame["High"] + frame["Low"] + frame["Close"]) / 3.0
    flow = typical * frame["Volume"]
    up = flow.where(typical > typical.shift(1), 0.0)
    down = flow.where(typical < typical.shift(1), 0.0)
    pos = up.rolling(period, min_periods=period).sum()
    neg = down.rolling(period, min_periods=period).sum()
    out = 100.0 - 100.0 / (1.0 + pos / neg.replace(0.0, np.nan))
    return out.where(~((neg == 0) & (pos > 0)), 100.0)


_COMPUTE: dict[str, Callable[[pd.DataFrame], pd.Series]] = {
    "close_10_ema": lambda f: ema(f["Close"], 10),
    "close_20_ema": lambda f: ema(f["Close"], 20),
    "close_20_sma": lambda f: sma(f["Close"], 20),
    "close_50_sma": lambda f: sma(f["Close"], 50),
    "close_200_sma": lambda f: sma(f["Close"], 200),
    "macd": lambda f: macd(f["Close"])["macd"],
    "macds": lambda f: macd(f["Close"])["macds"],
    "macdh": lambda f: macd(f["Close"])["macdh"],
    "rsi": lambda f: rsi(f["Close"]),
    "boll": lambda f: bollinger(f["Close"])["boll"],
    "boll_ub": lambda f: bollinger(f["Close"])["boll_ub"],
    "boll_lb": lambda f: bollinger(f["Close"])["boll_lb"],
    "atr": atr,
    "vwma": vwma,
    "mfi": mfi,
}

INDICATOR_DESCRIPTIONS = {
    "close_10_ema": "EMA 10: trung bình ngắn hạn nhạy, bắt nhịp đổi động lượng; dễ nhiễu khi đi ngang.",
    "close_20_ema": "EMA 20: trung bình ngắn-trung hạn, hay dùng làm hỗ trợ động trong xu hướng tăng.",
    "close_20_sma": "SMA 20: trung bình 20 ngày (đường giữa Bollinger).",
    "close_50_sma": "SMA 50: xu hướng trung hạn, hỗ trợ/kháng cự động; trễ so với giá.",
    "close_200_sma": "SMA 200: thước đo xu hướng dài hạn; giá trên/dưới SMA200 là chế độ tăng/giảm phổ biến.",
    "macd": "MACD (12/26): động lượng từ chênh lệch hai EMA; theo dõi giao cắt và phân kỳ.",
    "macds": "Đường tín hiệu MACD (EMA 9 của MACD).",
    "macdh": "MACD histogram: MACD trừ đường tín hiệu, cho thấy động lượng mạnh lên hay yếu đi.",
    "rsi": "RSI 14 (Wilder): >70 quá mua, <30 quá bán; trong xu hướng mạnh RSI có thể neo ở vùng cực trị lâu.",
    "boll": "Bollinger giữa: SMA 20.",
    "boll_ub": "Bollinger trên: SMA 20 + 2 độ lệch chuẩn.",
    "boll_lb": "Bollinger dưới: SMA 20 - 2 độ lệch chuẩn.",
    "atr": "ATR 14 (Wilder): biên độ dao động thực trung bình theo USDT; crypto biến động mạnh nên ATR lớn.",
    "vwma": "VWMA 14: trung bình giá có trọng số khối lượng.",
    "mfi": "MFI 14: chỉ số dòng tiền dùng giá và khối lượng; >80 quá mua, <20 quá bán.",
}

SUPPORTED = tuple(_COMPUTE)


def compute(frame: pd.DataFrame, name: str) -> pd.Series:
    """Indicator ``name`` for every row of ``frame`` (columns of ``cryptotrader.data.klines.COLUMNS``)."""
    try:
        fn = _COMPUTE[name]
    except KeyError:
        raise ValueError(f"Indicator {name} is not supported. Choose from: {list(SUPPORTED)}") from None
    data = frame.reset_index(drop=True)
    for column in ("Open", "High", "Low", "Close", "Volume"):
        if column in data:
            data[column] = pd.to_numeric(data[column], errors="coerce").astype("float64")
    return fn(data).astype("float64")


def compute_many(frame: pd.DataFrame, names: Iterable[str]) -> pd.DataFrame:
    return pd.DataFrame({name: compute(frame, name) for name in names})


def load_frame(symbol: str, as_of_date: str) -> pd.DataFrame:
    """Closed candles through ``as_of_date`` (indirection kept so tests can replace it)."""
    from cryptotrader.data.ohlcv import load_ohlcv

    return load_ohlcv(symbol, as_of_date)


def get_indicator_window(symbol: str, indicator: str, as_of_date: str, look_back_days: int = 30) -> str:
    """One indicator for each closed candle of the ``look_back_days`` up to ``as_of_date``."""
    if indicator not in _COMPUTE:
        raise ValueError(f"Indicator {indicator} is not supported. Choose from: {list(SUPPORTED)}")
    pair = normalize_symbol(symbol)
    frame = load_frame(pair, as_of_date)
    try:
        values = compute(frame, indicator)
    except VendorError:
        raise
    except Exception as exc:  # noqa: BLE001 - a calculation failure is no data, not a crash
        raise NoMarketDataError(pair, f"{indicator} could not be computed: {exc}") from exc
    dates = pd.to_datetime(frame["Date"]).reset_index(drop=True)
    start = pd.Timestamp(as_of_date) - pd.Timedelta(days=max(1, int(look_back_days)))
    lines = []
    for day, value in sorted(zip(dates, values, strict=False), key=lambda x: x[0], reverse=True):
        if day < start:
            continue
        lines.append(f"{day:%Y-%m-%d}: {'N/A (chưa đủ dữ liệu)' if pd.isna(value) else fmt.price(value)}")
    return (f"## {indicator} của {pair} từ {start:%Y-%m-%d} đến {as_of_date} "
            f"(nến ngày UTC đã đóng):\n\n" + "\n".join(lines) + "\n\n" + INDICATOR_DESCRIPTIONS[indicator])

