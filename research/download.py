"""Download and update the research data set (incremental, resumable).

    python -m research download [--workers 8] [--refresh] [--limit N] [--symbols BTCUSDT,ETHUSDT] [--sha256]

Source: the Binance bulk archive (data.binance.vision), which keeps every spot
pair that ever traded, delisted ones included - the REST API forgets a pair
once it is delisted, so a universe built from it would only hold survivors.

1. The pair list: every ``*USDT`` prefix under ``data/spot/monthly/klines/``
   (plus ``daily/klines/`` for pairs too young for a monthly file), from the
   public S3 bucket listing -> ``archive_symbols.json``.
2. For each pair, daily candles: the monthly zips, then the daily zips for the
   days after the last monthly file (at most ``DAILY_WINDOW_DAYS`` past it: a
   pair whose monthly files stopped long ago is delisted)
   -> ``klines/{SYMBOL}.csv`` (``cryptotrader.data.klines`` columns).
3. Today's exchangeInfo statuses (data-api.binance.vision) -> ``exchange_status.json``,
   for the coverage report only (the backtest never reads it).

Every zip is verified before use: against the MD5 the bucket listing publishes
as its ETag (single-part uploads), and with ``--sha256`` (or when the ETag is
not a plain MD5) also against the published ``.CHECKSUM`` SHA-256. A mismatch
fails the pair; nothing unverified is written.

Incremental: ``klines/{SYMBOL}.json`` records every archive file already merged
with its ETag. A rerun lists each pair's files again (one request per pair, a
few more for pairs still trading) and downloads only the new or re-published
ones. Every file is written to a temp file and renamed (a killed run leaves
the old file or the new one), the CSV before its manifest (a crash between the
two only re-downloads files). Ctrl-C cancels the queued pairs, lets the
running ones finish, saves ``download_meta.json`` and exits 130; rerun to resume.

``download_meta.json`` records, per pair: first and last trading day (a candle
with volume), number of trading days, number of listing segments (a gap of
more than ``DELIST_GAP_DAYS`` days = delisted then relisted), whether it still
trades at the data end, and the error of a failed pair. Failures also go to
``download.log``. ``data_end`` (the panel's last day) moves only after a full
run (not ``--symbols`` / ``--limit`` / interrupted) in which no pair that still
traded at the previous data end failed: otherwise the pairs that were not
updated would look delisted on the newer days.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree

import pandas as pd

from cryptotrader.data import archive, klines as kl
from cryptotrader.data.errors import ArchiveChecksumError, VendorError, VendorUnavailableError
from cryptotrader.data.files import replace_file, write_text_atomic
from cryptotrader.data.net import http_get
from cryptotrader.market import rules
from research import config

log = logging.getLogger("research.download")

INTERRUPTED = 130                 # exit code after Ctrl-C (128 + SIGINT)
QUOTE = rules.QUOTE
INTERVAL = "1d"
MONTHLY_PREFIX = "data/spot/monthly/klines/"
DAILY_PREFIX = "data/spot/daily/klines/"
DAILY_WINDOW_DAYS = 62            # daily files are searched this far past the last monthly file
DEFAULT_WORKERS = 8
MAX_WORKERS = 16                  # be polite to the archive
TRIES = 4                         # attempts per request (on top of http_get's own retry of 429/5xx)
MAX_LISTING_PAGES = 200
ACTIVE_WITHIN_DAYS = 3            # a pair with a candle this recent still trades (the universe's rule)

_SYMBOL = re.compile(r"^[A-Z0-9]{2,30}$")
_MONTHLY = re.compile(r"-(\d{4})-(\d{2})\.zip$")
_DAILY = re.compile(r"-(\d{4})-(\d{2})-(\d{2})\.zip$")
_MD5 = re.compile(r"^[0-9a-f]{32}$")


class MissingFile(VendorError):
    """An archive file listed a moment ago answered 404."""


# ── HTTP with retries (no circuit breaker: one slow file must not stop a bulk run) ──
def _get(url: str, params: dict | None = None, allow_status: tuple[int, ...] = ()):
    last: Exception | None = None
    for attempt in range(TRIES):
        try:
            return http_get(url, params=params, allow_status=allow_status, retries=2, deadline=90.0)
        except VendorUnavailableError as exc:
            last = exc
            if attempt + 1 < TRIES:
                time.sleep(min(20.0, 1.5 * 2 ** attempt))
    raise last  # type: ignore[misc]


# ── the bucket listing ──
def parse_contents(xml: bytes | str) -> tuple[dict[str, str], list[str], bool, str | None]:
    """({key: etag}, common prefixes, is_truncated, next marker) of one S3 ListBucket page.
    The ETag is lower-case without quotes (the MD5 of the file for a single-part upload)."""
    root = ElementTree.fromstring(xml)
    ns = root.tag[: root.tag.index("}") + 1] if root.tag.startswith("{") else ""
    files: dict[str, str] = {}
    for item in root.findall(f"{ns}Contents"):
        key = item.findtext(f"{ns}Key")
        if key:
            files[key] = (item.findtext(f"{ns}ETag") or "").strip().strip('"').lower()
    prefixes = [p for cp in root.findall(f"{ns}CommonPrefixes") if (p := cp.findtext(f"{ns}Prefix"))]
    truncated = (root.findtext(f"{ns}IsTruncated") or "").strip().lower() == "true"
    marker = root.findtext(f"{ns}NextMarker") or None
    if truncated and not marker:
        marker = (list(files) or prefixes or [None])[-1]
    return files, prefixes, truncated, marker


def list_bucket(prefix: str, *, delimiter: bool = False) -> tuple[dict[str, str], list[str]]:
    """Every key (with its ETag) and common prefix under ``prefix``, all pages."""
    files: dict[str, str] = {}
    prefixes: list[str] = []
    marker: str | None = None
    for _ in range(MAX_LISTING_PAGES):
        params = {"prefix": prefix}
        if delimiter:
            params["delimiter"] = "/"
        if marker:
            params["marker"] = marker
        result = _get(archive.LISTING_URL, params=params)
        try:
            page, page_prefixes, truncated, marker = parse_contents(result.content)
        except ElementTree.ParseError as exc:
            raise VendorUnavailableError(f"S3 listing of {prefix!r} unreadable ({exc})") from None
        files.update(page)
        prefixes += page_prefixes
        if not truncated or not marker:
            return files, prefixes
    raise VendorUnavailableError(f"S3 listing of {prefix!r}: more than {MAX_LISTING_PAGES} pages")


def _pairs_under(prefix: str) -> set[str]:
    _, prefixes = list_bucket(prefix, delimiter=True)
    names = {p.rstrip("/").rsplit("/", 1)[-1] for p in prefixes}
    return {s for s in names if s.endswith(QUOTE) and len(s) > len(QUOTE) and _SYMBOL.match(s)}


def list_symbols() -> list[str]:
    """Every ``*USDT`` spot pair the archive has daily-or-longer klines for (delisted included)."""
    monthly = _pairs_under(MONTHLY_PREFIX)
    daily = _pairs_under(DAILY_PREFIX)
    symbols = sorted(monthly | daily)
    _write_json(config.symbols_file(), {
        "fetched": datetime.now().isoformat(timespec="seconds"), "quote": QUOTE,
        "monthly": sorted(monthly), "daily_only": sorted(daily - monthly)})
    return symbols


# ── files ──
def _month_of(key: str) -> date | None:
    m = _MONTHLY.search(key)
    return date(int(m.group(1)), int(m.group(2)), 1) if m else None


def _day_of(key: str) -> date | None:
    m = _DAILY.search(key)
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def _month_end(first: date) -> date:
    nxt = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
    return nxt - timedelta(days=1)


def _months(first: date, last: date) -> list[str]:
    out, cur = [], date(first.year, first.month, 1)
    while cur <= last:
        out.append(cur.strftime("%Y-%m"))
        cur = _month_end(cur) + timedelta(days=1)
    return out


def fetch_zip(key: str, etag: str, *, sha256: bool = False) -> pd.DataFrame:
    """One archive zip as candles, verified (ETag MD5 and/or published SHA-256) before use."""
    result = _get(f"{archive.ARCHIVE_URL}/{key}", allow_status=(404,))
    if result.status == 404:
        raise MissingFile(f"{key}: 404")
    data = result.content
    md5_ok = False
    if _MD5.match(etag or ""):
        actual = hashlib.md5(data).hexdigest()
        if actual != etag:
            raise ArchiveChecksumError(f"{key}: MD5 {actual} != listed ETag {etag}")
        md5_ok = True
    if sha256 or not md5_ok:
        check = _get(f"{archive.ARCHIVE_URL}/{key}.CHECKSUM", allow_status=(403, 404))
        if check.status == 200:
            archive.verify_checksum(data, check.text)
        elif not md5_ok:
            raise ArchiveChecksumError(f"{key}: no MD5 ETag and no .CHECKSUM file: cannot verify")
    return archive.read_zip_klines(data)


def _read_frame(path: Path) -> pd.DataFrame | None:
    """The saved candles, or None when missing / empty / unreadable (fetched again)."""
    try:
        frame = pd.read_csv(path)
    except (OSError, ValueError):
        return None
    if "Date" not in frame.columns or "Close" not in frame.columns:
        return None
    return kl.clean(frame)


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data) -> None:
    write_text_atomic(path, json.dumps(data, indent=1, ensure_ascii=False))


def _write_frame(path: Path, frame: pd.DataFrame) -> None:
    out = frame.copy()
    out["Date"] = out["Date"].dt.strftime("%Y-%m-%d")
    replace_file(path, lambda tmp: out.to_csv(tmp, index=False))


def summarize(frame: pd.DataFrame, end: date) -> dict:
    """First/last trading day (a candle with volume), trading days, listing segments, still active."""
    traded = frame[frame["Volume"] > 0] if not frame.empty else frame
    if traded.empty:
        return {"first_day": None, "last_day": None, "bars": 0, "segments": 0, "active": False,
                "zero_volume_days": int(len(frame))}
    gaps = traded["Date"].diff().dt.days.fillna(0)
    last = traded["Date"].iloc[-1].date()
    return {
        "first_day": str(traded["Date"].iloc[0].date()),
        "last_day": str(last),
        "bars": int(len(traded)),
        "segments": int((gaps > config.DELIST_GAP_DAYS).sum()) + 1,
        "active": (end - last).days < ACTIVE_WITHIN_DAYS,
        "zero_volume_days": int(len(frame) - len(traded)),
    }


def plan_files(symbol: str, end: date, manifest: dict[str, str]) -> list[tuple[str, str]]:
    """The archive files (key, etag) of ``symbol`` not merged yet (or re-published), daily first."""
    listed, _ = list_bucket(f"{MONTHLY_PREFIX}{symbol}/{INTERVAL}/")
    monthly = {k: e for k, e in listed.items() if (m := _month_of(k)) is not None and m <= end}
    last_month_end = max((_month_end(_month_of(k)) for k in monthly), default=None)
    if last_month_end is None:
        daily_from = end - timedelta(days=DAILY_WINDOW_DAYS)
    elif (end - last_month_end).days <= DAILY_WINDOW_DAYS:
        daily_from = last_month_end + timedelta(days=1)
    else:
        daily_from = None                    # monthly files stopped long ago: delisted
    daily: dict[str, str] = {}
    if daily_from is not None and daily_from <= end:
        for month in _months(daily_from, end):
            found, _ = list_bucket(f"{DAILY_PREFIX}{symbol}/{INTERVAL}/{symbol}-{INTERVAL}-{month}")
            daily.update({k: e for k, e in found.items()
                          if (d := _day_of(k)) is not None and daily_from <= d <= end})
    todo = [(k, e) for k, e in sorted(daily.items()) if manifest.get(k.rsplit("/", 1)[-1]) != e]
    todo += [(k, e) for k, e in sorted(monthly.items()) if manifest.get(k.rsplit("/", 1)[-1]) != e]
    return todo


def update_symbol(symbol: str, end: date, *, refresh: bool = False, sha256: bool = False) -> dict:
    """Bring ``klines/{symbol}.csv`` up to ``end`` from the archive; a summary dict (``ok``)."""
    if not _SYMBOL.match(symbol):
        return {"symbol": symbol, "ok": False, "error": "invalid symbol name"}
    kdir = config.klines_dir()
    csv_path, man_path = kdir / f"{symbol}.csv", kdir / f"{symbol}.json"
    old = None if refresh else _read_frame(csv_path)
    manifest = {} if old is None else dict(_read_json(man_path).get("files") or {})
    try:
        todo = plan_files(symbol, end, manifest)
        frames = []
        for key, etag in todo:
            frames.append(fetch_zip(key, etag, sha256=sha256))
            manifest[key.rsplit("/", 1)[-1]] = etag
    except (VendorError, ValueError, OSError) as exc:
        return {"symbol": symbol, "ok": False, "error": f"{type(exc).__name__}: {exc}"[:500]}
    frame = kl.merge(old, pd.concat(frames, ignore_index=True)) if frames else (old if old is not None else kl.empty_frame())
    frame = frame[frame["Date"] <= pd.Timestamp(end)].reset_index(drop=True)
    if frame.empty:
        return {"symbol": symbol, "ok": False, "error": "no daily candles in the archive"}
    if frames:
        # Daily files of a month now covered by its monthly file are superseded: forget them.
        months = {n[-11:-4] for n in manifest if _MONTHLY.search(n)}
        manifest = {n: e for n, e in manifest.items() if not (_DAILY.search(n) and n[-14:-7] in months)}
        _write_frame(csv_path, frame)
        _write_json(man_path, {"symbol": symbol, "updated": datetime.now().isoformat(timespec="seconds"),
                               "files": manifest})
    return {"symbol": symbol, "ok": True, "fetched": len(todo), **summarize(frame, end)}


def fetch_statuses() -> dict[str, str]:
    """Today's exchangeInfo status of every USDT pair (TRADING, BREAK, ...); {} when unavailable."""
    from cryptotrader.data import binance

    try:
        info = binance.fetch_exchange_info()
    except VendorError as exc:
        log.warning("exchangeInfo unavailable (%s): coverage report without today's statuses", exc)
        return {}
    out = {s: i.status for s, i in info.items() if i.quote == QUOTE}
    _write_json(config.exchange_info_file(), {"fetched": datetime.now().isoformat(timespec="seconds"),
                                              "status": out})
    return out


# ── the run ──
def _run_pool(fn, items, workers: int, handle) -> None:
    """``handle(item, future)`` for each ``fn(item)`` as it finishes, ``workers`` at a time.

    On Ctrl-C the queued tasks are cancelled, the running ones finish and are
    handled, then KeyboardInterrupt is raised again.
    """
    pool = ThreadPoolExecutor(max_workers=workers)
    futs = {pool.submit(fn, x): x for x in items}
    handled = set()
    try:
        for fut in as_completed(futs):
            handle(futs[fut], fut)
            handled.add(fut)
    except KeyboardInterrupt:
        log.warning("interrupted: cancelling the queued pairs, waiting for the running ones")
        pool.shutdown(wait=True, cancel_futures=True)
        for fut, item in futs.items():
            if fut not in handled and fut.done() and not fut.cancelled():
                handle(item, fut)
        raise
    pool.shutdown(wait=True)


def _setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in [h for h in root.handlers if getattr(h, "_research", False)]:
        root.removeHandler(h)
    for handler in (logging.StreamHandler(), logging.FileHandler(config.log_file(), encoding="utf-8")):
        handler.setFormatter(fmt)
        handler._research = True
        root.addHandler(handler)
    for noisy in ("cryptotrader", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def build_parser(ap: argparse.ArgumentParser | None = None) -> argparse.ArgumentParser:
    ap = ap or argparse.ArgumentParser(prog="python -m research download", description=__doc__.split("\n")[0])
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                    help=f"parallel pairs (max {MAX_WORKERS}, default {DEFAULT_WORKERS})")
    ap.add_argument("--refresh", action="store_true", help="download every file again")
    ap.add_argument("--limit", type=int, default=0, help="only the first N pairs (testing)")
    ap.add_argument("--symbols", default="", help="only these pairs, comma-separated (e.g. BTCUSDT,ETHUSDT)")
    ap.add_argument("--sha256", action="store_true",
                    help="also verify each zip against its published .CHECKSUM (twice the requests)")
    return ap


def _wanted(args) -> list[str]:
    return sorted({s.strip().upper() for s in (args.symbols or "").split(",") if s.strip()})


def coverage(results: dict, statuses: dict[str, str] | None = None) -> dict:
    """Counts for the README: pairs, still trading, delisted (no candle in the last days), relisted."""
    ok = {s: v for s, v in results.items() if v.get("ok") and v.get("bars")}
    out = {"pairs": len(ok), "active": sum(1 for v in ok.values() if v.get("active")),
           "inactive": sum(1 for v in ok.values() if not v.get("active")),
           "relisted": sum(1 for v in ok.values() if (v.get("segments") or 0) > 1),
           "excluded_by_rules": sum(1 for s in ok if not rules.is_tradable_pair(s)),
           "inactive_tradable": sum(1 for s, v in ok.items() if not v.get("active") and rules.is_tradable_pair(s))}
    if statuses:
        out["exchange_trading_now"] = sum(1 for s in ok if statuses.get(s) == "TRADING")
        out["not_in_exchange_info"] = sum(1 for s in ok if s not in statuses)
    return out


def main(argv=None, args=None) -> int:
    args = args or build_parser().parse_args(argv)
    workers = max(1, min(int(args.workers), MAX_WORKERS))
    config.klines_dir().mkdir(parents=True, exist_ok=True)
    _setup_logging()
    end = rules.final_day()
    meta = _read_json(config.meta_file())
    prev_end = meta.get("data_end")
    wanted = _wanted(args)
    partial = bool(wanted or args.limit)
    log.info("data directory %s; candles up to %s (previous data end %s)", config.data_dir(), end, prev_end)
    meta["started"] = datetime.now().isoformat(timespec="seconds")
    results: dict[str, dict] = meta.get("symbols", {})
    done: dict[str, dict] = {}

    def save():
        meta["symbols"] = dict(sorted(results.items()))
        meta["failed"] = sorted(s for s, v in results.items() if not v.get("ok"))
        _write_json(config.meta_file(), meta)

    try:
        if wanted:
            symbols = wanted
        else:
            symbols = list_symbols()
            log.info("archive lists %d %s pairs", len(symbols), QUOTE)
            statuses = fetch_statuses()
            if statuses:
                meta["exchange_status_fetched"] = datetime.now().isoformat(timespec="seconds")
        if args.limit:
            symbols = symbols[: args.limit]
        # The benchmark first: without it there is no calendar.
        symbols = sorted(symbols, key=lambda s: (s not in (rules.BENCHMARK, "ETHUSDT"), s))
        log.info("updating %d pairs with %d workers", len(symbols), workers)
        t0 = time.time()

        def task(symbol):
            return update_symbol(symbol, end, refresh=args.refresh, sha256=args.sha256)

        def handle(symbol, fut):
            try:
                info = fut.result()
            except Exception as exc:  # noqa: BLE001 - a bug for one pair must not stop the run
                info = {"symbol": symbol, "ok": False, "error": f"{type(exc).__name__}: {exc}"[:500]}
            prev = results.get(symbol, {})
            if info.get("ok"):
                prev.pop("error", None)
                results[symbol] = {**prev, **info}
            else:
                log.error("%s failed: %s", symbol, info.get("error"))
                results[symbol] = {**prev, "ok": False, "error": info.get("error")}
            done[symbol] = info
            if len(done) % 50 == 0:
                log.info("%d/%d pairs (%.0fs)", len(done), len(symbols), time.time() - t0)
                save()

        _run_pool(task, symbols, workers, handle)
    except KeyboardInterrupt:
        meta["interrupted"] = datetime.now().isoformat(timespec="seconds")
        save()
        log.warning("interrupted after %d pairs; finished files are kept, data end stays %s. "
                    "Rerun `python -m research download` to continue.", len(done), prev_end)
        return INTERRUPTED
    except VendorError as exc:
        log.error("archive listing failed: %s (rerun later)", exc)
        save()
        return 1

    # Forget failures of pairs that have no file at all and were not tried this time (a typo).
    for s in [s for s, v in results.items() if not v.get("ok") and s not in done]:
        if not (config.klines_dir() / f"{s}.csv").exists():
            del results[s]
    lasts = [v["last_day"] for v in results.values() if v.get("ok") and v.get("last_day")]
    reached = max(lasts) if lasts else None
    # A pair that still traded at the previous data end and failed now was not updated:
    # moving the end past it would make it look delisted (dropped from the universe,
    # holdings liquidated at a stale close). The end waits until a rerun gets it.
    blocking = sorted(s for s, v in done.items() if not v.get("ok") and prev_end
                      and str(results.get(s, {}).get("last_day") or "") >= prev_end)
    if blocking and (not partial or prev_end is None):
        log.warning("data end stays %s: %d pair(s) still trading at it failed to update (%s); rerun "
                    "`python -m research download`", prev_end, len(blocking), ", ".join(blocking[:20]))
        meta["data_end_blocked_by"] = blocking
    elif not partial or prev_end is None:
        meta["data_end"] = reached or prev_end
        meta.pop("data_end_blocked_by", None)
    elif reached and reached > prev_end:
        log.info("partial run: data end stays %s (run without --symbols/--limit to move every pair)", prev_end)
    statuses = _read_json(config.exchange_info_file()).get("status") or {}
    meta["coverage"] = coverage(results, statuses)
    meta.pop("interrupted", None)
    meta["finished"] = datetime.now().isoformat(timespec="seconds")
    save()
    failed_now = sorted(s for s, v in done.items() if not v.get("ok"))
    log.info("done in %.0fs: %d ok, %d failed %s; data end %s; coverage %s", time.time() - t0,
             len(done) - len(failed_now), len(failed_now), failed_now[:20], meta.get("data_end"), meta["coverage"])
    if failed_now:
        log.info("rerun `python -m research download` to retry the failed pairs (see %s)", config.log_file())
    return 0


if __name__ == "__main__":
    sys.exit(main())
