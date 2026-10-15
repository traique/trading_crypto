# Hồ sơ kiểm định chiến lược crypto (15/10/2026)

Tài liệu này ghi lại lần kiểm định độc lập đã chọn chiến lược mà bot đang chạy (`btc_sma_ens`). Mọi con số ở đây là **sau chi phí**. Số TRAIN/VALID là **trong mẫu** (đã được nhìn khi thiết kế ứng viên); chỉ TEST là ngoài mẫu, và TEST chỉ được chạy **một lần**.

Bản gốc của quy tắc chọn, quyết định và code đã kiểm định nằm trong [`research/evaluated/`](evaluated/):

| tệp | nội dung |
|---|---|
| `protocol.md`, `protocol.sha256`, `timeline.log` | quy tắc chọn viết ra và băm **trước** khi chạy TEST; dòng thời gian |
| `winner.json` | quyết định, luật chính xác, chỉ số từng giai đoạn, phép thử sức chịu, bootstrap, phương án gần nhất |
| `trend_majors.py` | bản sao **giống từng byte** của code ứng viên mà người kiểm định đã băm và chạy |

Mã băm được kiểm tra trong `tests/test_strategy_sma_ensemble.py`.

## 1. Thiết lập

- **Người kiểm định** không thiết kế ứng viên nào. 12 ứng viên từ 5 nhóm nghiên cứu (trend-majors, risk-overlays, factor-books, portfolio-blend, xs-momentum; nhóm cuối không nộp ứng viên).
- **Quy tắc chọn** (`protocol.md`): mục 0–7 đóng băng lúc 2026-10-15T12:05:35Z (sha256 `20b24658…`); phụ lục trước TEST (mục 8) đóng băng lúc 12:21:32Z.
- **TEST** chạy một lần, một đợt, 12:21:40–12:24:01Z: 396 lần chạy, 0 lỗi.
- **Dữ liệu:** kho lưu trữ Binance spot, 763 cặp USDT **kể cả cặp đã huỷ niêm yết**, nến tới 14/10/2026.
- **Giai đoạn:** TRAIN 01/01/2018–31/12/2021; VALID 01/01/2022–31/12/2023; TEST 01/01/2024–14/10/2026 (1.018 ngày).
- **Chi phí trong mọi con số:** phí 0,1% mỗi lần khớp, trượt giá 0,1%, khớp ở giá mở cửa ngày hôm sau, vốn đầu 10.000 USDT, tiền mặt lãi 0%.
- **Cổng chọn:** G0 kiểm toán (tái lập số, không nhìn trước, không dùng dữ liệu cấm); G1 MDD ≥ −50%; G2 Sharpe > BTC; G3 thắng `btc_trend` (chiến lược giữ chỗ SMA200); G4 phép thử sức chịu (trượt giá ×2, vốn 1k/100k/1M với trần 1% khối lượng, dời ngày bắt đầu 1–3 tháng, phí 0,075%). Tier 1 = quy tắc chặt; Tier 2 = bản nới; dự phòng = "quy tắc đơn giản nhất qua G0, G1, G2 và G4".

## 2. Quyết định

Không ứng viên nào qua Tier 1 hay Tier 2. Quy tắc dự phòng đã đăng ký trước chọn **`C2_btc_sma_ens`** (4 tham số) trong số các ứng viên đủ điều kiện: C2_btc_sma_ens (4), A_btc_trend_voltarget (6), C3_majors_sma_ens_vt50 (6), C2_LV_ENS_CORE50 (7). `btc_trend` không đủ điều kiện: Sharpe TEST 0,677 thấp hơn 0,703 của BTC, trượt G4 và trượt cổng MDD trên TRAIN (−64,7%).

```
tier=fallback winner=C2_btc_sma_ens
tier1: no candidate passes
tier2: no candidate passes
fallback: qualifiers ['C2_btc_sma_ens', 'A_btc_trend_voltarget', 'C3_majors_sma_ens_vt50', 'C2_LV_ENS_CORE50']; chosen C2_btc_sma_ens
```

### Luật (nguyên văn `winner.json`)

1. Decide once a day at the close of UTC day D (candle closes 00:00 UTC = 07:00 Vietnam time), using BTCUSDT daily closes up to and including D.
2. If BTCUSDT has no candle on D: change nothing.
3. Warm-up: with fewer than 201 closes the target weight is 0 (history window used: 203 closes).
4. For each n in (50, 75, 100, 125, 150, 175, 200): vote_n = 1 if close_D > mean of the last n closes (D included), else 0.
5. Target BTC weight w* = (sum of votes) / 7, so steps of 1/7; the rest of the NAV stays in USDT. No leverage, no shorting.
6. Let w = current BTC weight at D's close. If (w > 0.005) differs from (w* > 0.005), trade to w*. Otherwise trade to w* only when |w - w*| > 0.10; else no order.
7. Orders are market orders filled at D+1's open, planned with ledger.plan_target_weights (min_trade_pct 0.5 in the research form; the bot's default 1.0 gives identical results on every split).
8. Costs assumed in all numbers: 0.1% taker fee per fill + 0.1% slippage per fill; cash earns 0.

### Tóm tắt của người kiểm định (nguyên văn)

- **Kết luận:** không ứng viên nào qua được quy tắc chọn đã đăng ký trước, cả bản chặt (Tier 1) lẫn bản nới (Tier 2). Theo quy tắc dự phòng đã viết sẵn, người kiểm định chọn **C2_btc_sma_ens** (nhóm trend-majors).
- **Ý nghĩa trung thực:**
  - lợi nhuận gần bằng BTC, nhưng sụt giảm chỉ khoảng một nửa; C2 đứng ngoài phần lớn đợt BTC giảm 53 % (10/2025 → 06/2026);
  - phần vượt BTC (+0,7 điểm/năm) **không có ý nghĩa thống kê**: khoảng bootstrap 90 % là −38…+30 điểm;
  - thua BTC trong năm tăng mạnh: 2024 được +68 đến +76 %, so với +112 đến +121 % của BTC;
  - thua `btc_trend` trên VALID (2022–2023), vì SMA200 đứng ngoài trọn năm 2022;
  - thắng `btc_trend` trên TRAIN, TEST và toàn giai đoạn 2018–2026: CAGR 40,1 % so với 25,1 %, sụt giảm −46,9 % so với −64,7 %.
- **Phương án gần nhất:** `A_btc_trend_voltarget` có TEST nhỉnh hơn một chút (CAGR 26,3 %, sụt giảm −25,9 %, Sharpe 0,90).
  - A có 6 tham số so với 4 của C2, và trượt Tier 2 chỉ 0,02 điểm sụt giảm.
  - Hai chiến lược không phân biệt được về mặt thống kê.
- **Khuyến nghị:**
  - thay chiến lược giữ chỗ SMA200 của bot bằng C2_btc_sma_ens; bản `decide()` cho kết quả y hệt bản nghiên cứu;
  - chạy ở chế độ giấy/tín hiệu trước, rồi đánh giá lại sau 6–12 tháng dữ liệu mới;
  - lợi thế chính là tránh sập, không phải thắng BTC mỗi năm.

## 3. Kết quả của chiến lược được chọn (sau chi phí)

| giai đoạn | CAGR % | MDD % | Sharpe | lệnh khớp | BTC (giá, không phí): CAGR / MDD / Sharpe | `btc_trend`: CAGR / MDD / Sharpe |
|---|---|---|---|---|---|---|
| TRAIN 2018–2021 (trong mẫu) | 58,4 | −41,4 | 1,19 | 217 | 36,3 / −81,2 / 0,80 | 21,1 / −64,7 / 0,63 |
| VALID 2022–2023 (một phần trong mẫu) | 25,3 | −21,5 | 0,93 | 92 | −5,9 / −66,9 / 0,17 | 39,0 / −18,9 / 1,27 |
| **TEST 2024 – 14/10/2026 (ngoài mẫu, chạy một lần)** | **25,4** | **−29,6** | **0,86** | 180 | 24,7 / −53,0 / 0,70 | 19,7 / −32,3 / 0,68 |
| VALID+TEST liên tục | 26,6 | −29,6 | 0,91 | 271 | 11,9 / −66,9 / 0,47 | 28,6 / −32,3 / 0,92 |
| Toàn bộ 2018 – 14/10/2026 | 40,1 | −46,9 | 1,04 | 488 | 22,9 / −81,2 / 0,65 | 25,1 / −64,7 / 0,74 |

Trên TEST, C2 đầu tư trung bình 58% NAV; phí bằng 5,5% vốn đầu trong 2,8 năm (vòng quay một chiều 6,3 lần/năm, kéo CAGR xuống khoảng 2,5 điểm/năm ở mức 0,2% mỗi chiều).

### Lợi nhuận theo năm % (chạy liên tục 2018 → 14/10/2026; 2026* tới 14/10)

| | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026* |
|---|---|---|---|---|---|---|---|---|---|
| C2_btc_sma_ens | −28,1 | 99,4 | 196,3 | 48,0 | −21,1 | 98,8 | 76,3 | −1,0 | 12,6 |
| `btc_trend` | −35,1 | 56,5 | 172,7 | −22,4 | 0,0 | 93,3 | 79,9 | −18,4 | 17,6 |
| giá BTC | −72,3 | 94,3 | 302,0 | 59,8 | −64,2 | 155,6 | 121,3 | −6,3 | −6,8 |

C2 thắng BTC các năm 2018, 2019, 2022, 2025 và 2026 tới nay; thua các năm 2020, 2021, 2023, 2024. Lợi thế của nó là **mất ít hơn trong năm xấu**. Tính riêng trên TEST, 2024 được +68,4% so với +111,8% của BTC.

### Phép thử sức chịu trên TEST (CAGR % / MDD % / Sharpe; dòng cuối: Sharpe của BTC cùng những ngày đó)

| | gốc | trượt giá ×2 | vốn 1k | 100k, trần 1% KL | bắt đầu +1 tháng | +2 tháng | +3 tháng | phí 0,075% | 1M, trần 1% KL |
|---|---|---|---|---|---|---|---|---|---|
| C2_btc_sma_ens | 25,4 / −29,6 / 0,86 | 23,8 / −30,5 / 0,82 | 25,4 / −29,6 / 0,86 | 25,4 / −29,6 / 0,86 | 28,3 / −29,6 / 0,94 | 12,3 / −29,6 / 0,53 | 8,0 / −28,2 / 0,41 | 25,8 / −29,4 / 0,87 | 25,4 / −29,6 / 0,86 |
| Sharpe BTC | 0,70 | 0,70 | 0,70 | 0,70 | 0,74 | 0,45 | 0,36 | 0,70 | 0,70 |

Ngày bắt đầu ảnh hưởng rất mạnh tới **mọi** chiến lược: BTC tăng khoảng 44% trong tháng 2/2024, nên bắt đầu TEST từ 01/03 hoặc 01/04/2024 làm CAGR của mọi chiến lược còn 2–16%. C2 vẫn có Sharpe cao hơn BTC ở các mốc đó (0,53 so với 0,45; 0,41 so với 0,36); `btc_trend` thì không.

### Độ bất định (bootstrap khối dừng, khối trung bình 20 ngày, 5.000 lần; phân tích sau quyết định)

| so sánh (giai đoạn) | điểm | khoảng 90% | tỷ lệ lần rút > 0 |
|---|---|---|---|
| C2 − BTC, CAGR (TEST) | +0,7 | −38,2 … +29,6 | 0,50 |
| C2 − BTC, Sharpe (TEST) | +0,16 | −0,44 … +0,73 | 0,63 |
| C2 − `btc_trend`, CAGR (TEST) | +5,7 | −9,3 … +21,0 | 0,72 |
| A − C2, CAGR (TEST) | +0,9 | −5,7 … +7,0 | 0,59 |
| C2 − BTC, Sharpe (VALID+TEST) | +0,44 | −0,09 … +0,94 | 0,91 |
| C2 − `btc_trend`, CAGR (VALID+TEST) | −2,1 | −14,3 … +9,3 | 0,36 |

Khoảng 2,8 năm dữ liệu ngày không đủ để phân biệt các quy tắc xu hướng này với nhau, và chỉ phân biệt yếu với BTC khi tính theo rủi ro. Điều đứng vững trên cả TRAIN, VALID và TEST là **giảm sụt giảm** (−30 tới −47% so với −53 tới −81% của BTC). **Lợi nhuận vượt BTC không được dữ liệu ủng hộ.**

## 4. Toàn bộ ứng viên trên TEST (sau chi phí; chạy một lần)

| chiến lược | nhóm | CAGR % | MDD % | Sharpe | lệnh | phí % vốn | đầu tư TB % |
|---|---|---|---|---|---|---|---|
| **C2_btc_sma_ens** | trend-majors | **25,4** | **−29,6** | **0,86** | 180 | 5,5 | 57,8 |
| C3_majors_vt50 | trend-majors | 21,7 | −29,8 | 0,81 | 213 | 3,4 | 48,6 |
| C1_majors | trend-majors | 21,5 | −31,3 | 0,77 | 213 | 3,6 | 53,4 |
| C1_btc_trend_vt | risk-overlays | 20,1 | −32,0 | 0,70 | 30 | 3,9 | 58,1 |
| C2_stop_basket_iv | risk-overlays | 14,4 | −48,5 | 0,53 | 487 | 4,3 | 67,7 |
| C3_stop_basket | risk-overlays | 3,6 | −60,3 | 0,31 | 444 | 4,3 | 67,0 |
| C2_LV_ENS_CORE50 | factor-books | 22,5 | −30,7 | 0,77 | 241 | 2,5 | 58,1 |
| C1_LV_ENS | factor-books | 22,6 | −40,4 | 0,72 | 248 | 2,9 | 58,3 |
| C3_LV_PURE | factor-books | 21,8 | −55,4 | 0,64 | 135 | 2,5 | 99,6 |
| A_btc_trend_vt | portfolio-blend | 26,3 | −25,9 | 0,90 | 70 | 3,1 | 55,7 |
| B8_btc80_xs20 | portfolio-blend | 18,8 | −35,5 | 0,67 | 846 | 4,7 | 55,5 |
| B_btc60_eth20_xs20 | portfolio-blend | 19,4 | −37,1 | 0,69 | 909 | 4,6 | 52,5 |
| btc_buy_hold | mốc | 24,6 | −53,0 | 0,70 | 1 | 0,1 | 99,9 |
| `btc_trend` (giữ chỗ) | mốc | 19,7 | −32,3 | 0,68 | 29 | 3,9 | 60,5 |
| ETH mua và giữ | mốc | 1,0 | −67,5 | 0,35 | 1 | 0,1 | 99,9 |
| chia đều `liq_top10` | mốc | −5,3 | −69,8 | 0,24 | 244 | 1,5 | 99,3 |
| `xs_momentum` `liq_top20` | mốc | −34,6 | −83,2 | −0,14 | 232 | 2,2 | 98,6 |

Bối cảnh TEST: BTC từ 44,2k (01/01/2024) lên đỉnh 124,7k (06/10/2025), giảm 53% về 58,6k (30/06/2026), kết thúc ở 81,7k. Rổ altcoin (chia đều, động lượng chéo) thua xa BTC trên TEST, kể cả khi dữ liệu đã gồm cặp bị huỷ niêm yết.

## 5. Đối chiếu bản bot với code đã kiểm định

Bot chạy `cryptotrader/strategies/sma_ensemble.py` (`BtcSmaEnsemble`, tên `btc_sma_ens`), viết lại từ luật ở trên, không import code nghiên cứu. Hai phép kiểm tra:

1. **Panel tổng hợp** (`tests/test_strategy_sma_ensemble.py`, chạy trong bộ test mặc định): 2 chuỗi giá ngẫu nhiên 1.300 ngày có pha tăng/giảm và vài ngày thiếu nến; bản kiểm định (`btc_sma_ens_eval`) và bản bot (`btc_sma_ens`) chạy qua cùng bộ backtest → **mọi lệnh khớp (ngày, cặp, chiều, khối lượng, giá, phí) và NAV từng ngày giống hệt**, cả khi bot lập lệnh với mức tối thiểu 1% NAV.
2. **Dữ liệu thật** (`python -m research.parity`, ngày 15/10/2026, TEST vẫn khoá):

| giai đoạn | lệnh khớp | giống hệt | giống hệt với mức tối thiểu 1% của bot | max \|ΔNAV\| | CAGR % / MDD % / Sharpe (bản bot) | số của người kiểm định |
|---|---|---|---|---|---|---|
| TRAIN | 217 | có | có | 0 | 58,35 / −41,44 / 1,189 | 58,35 / −41,44 / 1,189 / 217 |
| VALID | 92 | có | có | 0 | 25,28 / −21,54 / 0,931 | 25,28 / −21,54 / 0,931 / 92 |
| TRAIN+VALID | 309 | có | có | 0 | 46,46 / −46,85 / 1,102 | 46,46 / −46,85 / 1,102 / 309 |

Người kiểm định cũng đã xác nhận bản `decide()` cho NAV y hệt bản nghiên cứu trên TEST. Bản bot không được chạy lại trên TEST (không mở khoá TEST lần hai).

## 6. Giới hạn trung thực (nguyên văn báo cáo người kiểm định, mục 11)

> - **One market path.**
>   - TEST is about 2.8 years: one bull run, one top and one bear market.
>   - No conclusion here is statistically strong.
> - **Selection effects.**
>   - Within the fallback, the pick was made by parameter count, not by TEST performance, so the winner's TEST numbers carry little winner's-curse bias. The TEST gates still act as a mild filter.
>   - VALID is partly in-sample: trend-majors chose C2 over its BTC+ETH siblings because VALID favoured it.
>   - The trend-majors family searched 805 configurations, so its TRAIN and VALID numbers are optimistic.
> - **The placeholder comparison is not won outright.**
>   - C2 does not beat the SMA200 placeholder on VALID or on VALID+TEST.
>   - It is recommended because the placeholder fails the protocol's TRAIN drawdown gate and TEST risk-adjusted gate, and C2 is the simplest rule that passes everything except that comparison.
> - **Costs.**
>   - The 0.1 % fee and 0.1 % slippage are conservative for BTCUSDT at small size.
>   - Idle USDT earns nothing in the backtest. On average about 42 % of C2's NAV sits in USDT; C2 is fully in cash (5 % of NAV or less in coins) on about 25 % of TEST days. *[Editor's correction from `winner.json` TEST: exposure 57.8 %, time invested 74.6 %. The original wording, "in cash about 42 % of the time", confused the average cash weight with the share of days in cash.]*
>   - No taxes, and no exchange or operational failures are modelled.
> - **Data.**
>   - Daily bars only, with fills at the open.
>   - No historical exchange filters; this is irrelevant for BTC at these sizes.
>   - Survivorship is minimal for a BTC-only rule.
> - **The rejected basket candidates were treated optimistically.** Delisted pairs are sold at their last close, and renamed tokens count as delistings. They still did not beat the BTC-only rules on TEST.
> - **xs-momentum** submitted no candidate. Its unfinished grid was not used.
> - **Report delivery.** The protocol said `report.md` would be written to disk. The harness forbids subagent report files, so the report is delivered as this answer. This is logged in `timeline.log`.

Thêm từ phía bot:

- **Khớp lệnh thật lệch giờ:** backtest và tài khoản ảo khớp ở giá mở cửa 00:00 UTC; người dùng làm theo tín hiệu đặt lệnh sau báo cáo 07:10 giờ VN, nên giá thật lệch khỏi giá mở cửa đó. Chỉ có giả định trượt giá 0,1% che phần này.
- **TEST tiếp theo là dữ liệu mới:** 6–12 tháng tới (sau 14/10/2026) là phép thử thật sự mới đầu tiên. Tài khoản ảo của bot ghi lại đúng phép thử đó.
