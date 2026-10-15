# Evaluation protocol: crypto research candidates (independent evaluator)

- **Written:** 2026-10-15T12:05Z (UTC), before any run that touches a date after 2023-12-31.
- **Frozen by:** `protocol.sha256` (written right after this file) and the log line in `timeline.log`.
- **Rule:** nothing in sections 1-6 may change after this file is written. Section 8 ("pre-TEST addendum") may only add audit verdicts, free-parameter counts and the exact way each candidate is invoked, and it must be written before TEST is unlocked.

## 0. What the evaluator had seen when writing this

- The four research reports in the brief (self-reported TRAIN/VALID numbers and notes), `baselines.md` (TRAIN, VALID, TRAIN+VALID only), and the harness source (`research/*.py`).
- Data metadata: `data_end` = 2026-10-14. While checking that date, the last five BTCUSDT daily candles (2026-10-10..14, close about 81.7k) were printed. No strategy has been run on any day after 2023-12-31, and no other TEST-period data has been inspected.
- The evaluator is a language model with general background knowledge of crypto prices that overlaps the start of the TEST period. That cannot be removed. To limit its influence, the rule below keeps the structure of the orchestrator's suggested rule and the research families' own suggestions, and uses round, conventional thresholds (50 % drawdown, 20 %, 5 points, 0.10 Sharpe) rather than values tuned to any expectation about TEST.

## 1. Data, splits, costs

- Data: `CRYPTO_RESEARCH_DATA=/tmp/claude-0/-home-user-trading-vn/3ff16925-6a16-566a-8fc4-7f2cd08c9bba/scratchpad/crypto_research/data` (Binance archive, 763 USDT pairs including delisted ones, `data_end` 2026-10-14).
- Splits (fixed): TRAIN 2018-01-01..2021-12-31; VALID 2022-01-01..2023-12-31; TEST 2024-01-01..2026-10-14 (latest day in the data).
- Continuous runs: TRAIN+VALID (pre-TEST); VALID+TEST (2022-01-01..latest) and ALL (2018-01-01..latest), both in the TEST batch.
- Base settings for every run: capital 10,000 USDT; fee 0.1 % per fill (Binance taker, no BNB discount); slippage 0.1 % per market fill; fills at the next day's open; harness defaults otherwise (`rules.DEFAULT_FILTERS`, delisted holdings sold at their last close). Cash earns 0 %.
- Every run goes through `research.backtest.run` (the project's real `Ledger`), with the candidate's own code, imported from its research folder and not modified.
- Benchmarks:
  - BTC buy & hold = BTCUSDT price with no costs (the `benchmark` of the same run);
  - `btc_buy_hold` (with costs);
  - `btc_trend`, the bot's SMA200 placeholder (with costs), run with identical settings on identical days.

## 2. Candidates and baselines

Candidates (12):

| family | candidate | declared free params |
|---|---|---|
| trend-majors | C2_btc_sma_ens (family's primary) | 3 |
| trend-majors | C3_majors_sma_ens_vt50 | 5 |
| trend-majors | C1_majors_sma_ens | 4 |
| risk-overlays | C1_btc_trend_vt | 4 |
| risk-overlays | C2_monthly_stop_basket_iv | 4 |
| risk-overlays | C3_monthly_stop_basket | 2 |
| factor-books | C2_LV_ENS_CORE50 (family's first choice) | 7 |
| factor-books | C1_LV_ENS | 6 |
| factor-books | C3_LV_PURE | 4 |
| portfolio-blend | A_btc_trend_voltarget (family's default) | 4 |
| portfolio-blend | B8_btc_trend80_xs20 | 9 |
| portfolio-blend | B_btc60_eth20_xs20 | 10 |

The xs-momentum family submitted no candidate. Nothing from it is evaluated, and its unfinished grid is not used.

Baselines (every split, TEST included):

- BTCUSDT price, `btc_buy_hold`, `btc_trend`, ETH buy & hold, `ew` on liq_top10 (monthly), `xs_momentum` on liq_top20;
- for information only: the factor-books family's control, BTC with the same three-SMA regime (`lab.BtcRegime(regime_sma=[100,150,200], regime_band=0.03)`).

## 3. Procedure (the order is binding)

1. **Audit** (locked panel only, no TEST): read every candidate's code; run truncation tests; check survivorship and harness use; reproduce the reported TRAIN and VALID numbers.
2. **Pre-TEST addendum** (section 8, timestamped): G0 verdicts, free-parameter counts, and the exact invocation of each candidate.
3. **TEST batch**, run once by a single script (`test_batch.py`). For every candidate and baseline it runs:
   - TEST base;
   - the G4 stress variants on VALID and on TEST;
   - the VALID+TEST and ALL continuous runs;
   - the information-only variants: fee 0.075 %, and capital 1,000,000 USDT with a 1 % ADV cap.

   All results are written to disk and not re-run. If a run fails because of a bug in the evaluator's own script, the script may be fixed and only the failed runs re-run; this is logged. If a candidate's own code fails on TEST data, the candidate fails G0. No candidate code is changed after TEST is unlocked.
4. **Decision:** apply sections 4 and 5 mechanically, then write `report.md` and `winner.json`.

## 4. Gates (a candidate must pass every one)

- **G0, integrity** (decided before TEST):
  - **Truncation test.** On at least four dates D spread over TRAIN and VALID (month ends and crash days included), a run on the panel cut at D (`research.data.truncate`) must equal the run on the uncut panel stopped at D. Equal means the same daily NAV up to D (relative difference ≤ 1e-9) and the same pending orders decided at D (symbol, side, quantity). A longer run must also have the same NAV up to D.
  - **No future or full data.** No reads of `ctx._panel`, `research.data._FULL`, the panel pickle, the klines CSVs, `exchange_status.json`, `download_meta.json` or today's exchangeInfo to choose pairs or signals. Universes come only from `ctx.universe()` or point-in-time data.
  - **No harness misuse.** No fills at the decision day's close. No direct edits of ledger cash or positions. Costs are changed only through `run()` options.
  - **Reproduction.** Each reported TRAIN or VALID number that the evaluator cannot reproduce within 1.0 CAGR point, 1.0 drawdown point or 0.05 Sharpe is listed in the report. The evaluator's numbers are used in every case. Such a mismatch fails G0 only if the stated code and parameters cannot produce the reported strategy at all.
- **G1, drawdown:** max drawdown no worse than −50 % on TRAIN, on VALID and on TEST (base runs).
- **G2, beats BTC on a risk-adjusted basis:** Sharpe above BTC buy & hold's Sharpe (price, no costs, same days) on VALID and on TEST. Sharpe is the harness definition: daily returns, rf = 0, × √365.
- **G3, beats the placeholder:** on VALID and on TEST, compared with `btc_trend` run with identical settings, the candidate meets one of:
  - CAGR ≥ `btc_trend`'s CAGR; or
  - clearly lower drawdown: |MDD| ≤ 0.8 × |`btc_trend` MDD| AND Sharpe ≥ `btc_trend` Sharpe − 0.10.
- **G4, stable under stress:** on VALID and on TEST, under each variant below, the candidate keeps max drawdown ≥ −50 % and Sharpe above BTC's Sharpe over the same days. For variants (a)-(c), its CAGR must also stay within max(3.0 points, 20 % of |base CAGR|) of its base CAGR on that split.
  - (a) slippage × 2;
  - (b) capital 1,000 USDT;
  - (c) capital 100,000 USDT with `max_adv_pct` = 0.01;
  - (d), (e), (f) start moved 1, 2 and 3 calendar months later, same end.

  The base fee is already 0.1 %. The fee 0.075 % variant is information only.

## 5. Choice

- **Tier 1:** candidates that pass G0-G4.
  - Leader = the Tier-1 candidate with the highest TEST CAGR (base run).
  - Near-tie set = Tier-1 candidates whose TEST CAGR is within 5.0 points of the leader's.
  - Winner = the near-tie candidate with the fewest free parameters, then the shallowest TEST max drawdown, then the highest TEST Sharpe. The parameter count is the larger of the declared count and the evaluator's count (section 8).
- **Tier 2:** only if Tier 1 is empty.
  - The gates are the same, except that G3 is checked on TEST and on the continuous VALID+TEST run, instead of VALID and TEST separately.
  - The reason is fixed now: `btc_trend`'s VALID record rests on one decision. It stayed flat through all of 2022 and made 5 trades in two years. Beating it on VALID alone is therefore a single-episode test.
  - The winner is chosen as in Tier 1 and is labelled "Tier 2" in the report.
- **Fallback:** only if Tier 2 is empty. The "most robust simple rule" is chosen among `btc_trend` and every candidate that passes G0.
  - Each must pass G2, G1 on VALID and TEST, and G4. The TRAIN drawdown gate is dropped here because `btc_trend` itself fails it (TRAIN MDD −64.7 %), and the report must say so.
  - Pick the fewest parameters, then the highest min(VALID Sharpe, TEST Sharpe).
  - If nothing qualifies, the report says that no rule showed an edge over BTC buy & hold, and recommends keeping the bot in paper/signal mode.

## 6. Reported but not decisive

- The research families' own TEST suggestions:
  - trend-majors: C2's TEST MDD well below BTC's and its CAGR above `btc_trend`'s;
  - risk-overlays: C1 against `btc_trend` and `btc_buy_hold`;
  - factor-books: C1 and C2 against BTC with the same three-SMA regime;
  - portfolio-blend: prefer A unless B8 or B beats it by more than 3 CAGR points with MDD ≤ 40 %.
- Calendar-year returns against BTC; fee drag, exposure and turnover; TRAIN stress runs; the fee 0.075 % and capital 1,000,000 USDT (1 % ADV cap) variants.

## 7. Honesty commitments

- **TEST is run once, in one batch.** No candidate is changed afterwards. Nothing is re-selected outside sections 4 and 5.
- **The winner's TEST numbers are the out-of-sample estimate, with caveats.**
  - Choosing among several passers by TEST CAGR adds a small upward bias.
  - VALID is partly in-sample for several candidates; their authors say so.
  - TEST is about 2.8 years of one market path. Its CAGR has a standard error of roughly ±15-20 points for a BTC-like book, so close variants cannot be told apart. The near-tie and parameter rules exist for that reason.
- **Survivorship and costs.**
  - Delisted pairs are in the panel. Delisting exits at the last close are optimistic.
  - No historical exchange filters are available.
  - Fees and slippage are as in section 1; there is no funding, borrowing or leverage.

## 8. Pre-TEST addendum

Written 2026-10-15T12:21Z, after the audit and before TEST is unlocked. Nothing below changes a gate, a threshold or the choice rule.

### 8.1 Frozen code and exact invocation

- **Harness and package.** A snapshot of `research/` and `cryptotrader/` is in `eval/snapshot`, taken 2026-10-15T12:07Z; sha256 hashes are in `snapshot_sha256.txt`. The harness files were last modified 09:15-09:50, before any research run, so every family ran on this same code.
- **Candidate code.** Copied into `eval/cand_src`; sha256 hashes are in `cand_src_sha256.txt` and match the research folders byte for byte.
- **Evaluator scripts (sha256 prefix).** `evalcore.py` 242f67ce, `stress.py` 0afbc0d9, `gates.py` 3a291b22, `test_batch.py` 07ea8d8e.
- **Invocation.** Each candidate is run unmodified through `research.backtest.run`:

| candidate | invocation |
|---|---|
| C2_btc_sma_ens, C3_majors_sma_ens_vt50, C1_majors_sma_ens | `trend_majors.candidate(key)` (`on_day` form). The `decide()` form gives an identical NAV on TRAIN+VALID (checked). |
| C1_btc_trend_vt | `candidates.BtcTrendVolTarget()` (bot form). Identical NAV to `Overlay("btc_trend", vt 50/120, band 0.10)` (checked). |
| C2_monthly_stop_basket_iv, C3_monthly_stop_basket | `MonthlyStopBasketIV()` and `MonthlyStopBasket()` |
| factor-books candidates | `FactorBook(**factor_books.CANDIDATES[key])` |
| A, B8, B | `candidates.make("A" / "B8" / "B")` |
| control | `lab.BtcRegime(regime_sma=[100,150,200], regime_band=0.03)` |

### 8.2 G0 verdicts: all 12 candidates PASS

- **Reproduction.** Every reported TRAIN and VALID CAGR, MDD, Sharpe and trade count reproduces. The largest gap is 0.005 Sharpe, from rounding. Baselines match `baselines.md`.
- **Truncation tests: 224 of 224 identical.** There were 126 checks on 7 fixed dates (2018-11-30, 2019-06-30, 2020-03-12, 2021-05-19, 2022-05-31, 2022-11-09, 2023-10-29) and 98 checks on days each strategy actually decided trades. In 140 of the 224 checks, orders decided at D were pending. NAV, fills and pending orders at D were identical between the cut panel and the uncut panel, and the long runs matched up to D.
- **Static read.** No strategy code reads `ctx._panel`, `_FULL`, the pickle, CSVs, exchange files or the network. Two options touch more than the returned weights, and both are unused by the candidates:
  - `overlays.py` edits `led.cash` only inside the `stop_extra_slip` stress option. C2 and C3 use close-mode stops with `stop_extra_slip` = 0.
  - portfolio-blend's `filters_file` option (today's exchangeInfo step sizes) is unused.
- **Survivorship.** BTC and ETH as "the majors" is an ex-ante choice. Basket candidates use `ctx.universe(liq_topN)`, which is point-in-time and includes delisted pairs. Stablecoins, pegged and leveraged tokens are excluded by a static name list in `rules.py`. That list includes names that appeared only later, but excluding stablecoins is an ex-ante rule. This is noted, not disqualifying.
- **Porting notes** (not part of G0):
  - The bot calls `plan_target_weights` with the default `min_trade_pct` 1 %, while the harness uses the strategy's own value (trend-majors 0.5 %, portfolio-blend 2 %).
  - B8 and B need a point-in-time universe that `StrategyContext` does not provide today.

### 8.3 Free parameters (frozen)

Counting rule:

- every numeric constant of the rule counts 1. That includes non-default `min_trade_pct`, bands, vol targets and windows, stop %, caps, universe size, number of picks, sleeve weights and hysteresis;
- a lookback ensemble counts 2 (its shortest and longest member);
- schedule choices (daily / weekly / monthly) count 0;
- the count used is max(declared, evaluator).

| candidate | declared | evaluator | used |
|---|---|---|---|
| C2_btc_sma_ens | 3 | 4 | 4 |
| C3_majors_sma_ens_vt50 | 5 | 6 | 6 |
| C1_majors_sma_ens | 4 | 5 | 5 |
| C1_btc_trend_vt | 4 | 4 | 4 |
| C2_monthly_stop_basket_iv | 4 | 5 | 5 |
| C3_monthly_stop_basket | 2 | 3 | 3 |
| C2_LV_ENS_CORE50 | 7 | 7 | 7 |
| C1_LV_ENS | 6 | 6 | 6 |
| C3_LV_PURE | 4 | 3 | 4 |
| A_btc_trend_voltarget | 4 | 6 | 6 |
| B8_btc_trend80_xs20 | 9 | 11 | 11 |
| B_btc60_eth20_xs20 | 10 | 12 | 12 |
| btc_trend (placeholder) | 1 | 1 | 1 |

### 8.4 Status before TEST

From the TRAIN and VALID runs plus the VALID stress runs (`results/pre_test_*.json`):

- **Already out of every tier:**
  - C1_majors_sma_ens: G1, TRAIN MDD −52.09 %.
  - C2_monthly_stop_basket_iv and C3_monthly_stop_basket: G1 on TRAIN and VALID, G2 on VALID, G4 on VALID.
  - C3_LV_PURE: G1, TRAIN MDD −82.4 % and VALID MDD −54.0 %; G4 on VALID.
- **G3 on VALID** (`btc_trend` VALID: CAGR 39.05, MDD −18.89, Sharpe 1.268) is met only by C1_btc_trend_vt (39.70 / −18.12 / 1.287). Tier 1 can therefore only be C1_btc_trend_vt.
- **Still open for Tier 2**, depending on TEST: C2_btc_sma_ens, C3_majors_sma_ens_vt50, C1_btc_trend_vt, C2_LV_ENS_CORE50, C1_LV_ENS, A_btc_trend_voltarget, B8_btc_trend80_xs20, B_btc60_eth20_xs20.

### 8.5 Operational clarifications (fixed now)

- **G4 CAGR tolerance.** "Within max(3.0, 20 % of |base|)" is applied as written, i.e. symmetric: |Δ| ≤ tolerance.
- **TEST window.** TEST ends on the data's last day, 2026-10-14. The shifted TEST starts are 2024-02-01, 2024-03-01 and 2024-04-01.
- **BTC Sharpe in G2 and G4.** It is each run's own benchmark: BTCUSDT price over the same days, with no costs.
- **Dry run.** `test_batch.py` was dry-run on the LOCKED panel with a fake TEST window (2023-07-01..2023-12-31) to test the script. Those numbers are not TEST and are discarded (`eval/dryrun/`).
