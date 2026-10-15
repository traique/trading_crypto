# Đánh giá lại: lãi cao nhất có thể mà không cháy tài khoản

Ngày: 15/10/2026. Mọi số TEST là 01/2024–30/09/2026, dữ liệu futures thật (nến ngày + funding), chưa dùng để chọn tham số. Tái lập: `python -m research.optimize_futures`, `python -m research.futures_study --preset v2 [--dd-scale 0.3]`, bảng danh mục bằng bộ mô phỏng nhanh của `optimize_futures` (đã test cho cùng NAV với module thật).

## 1. Giới hạn cứng: lợi thế của chiến lược nhỏ

Chiến lược `ls_sma` v2 có Sharpe khoảng **0,35–1,0** trên dữ liệu mới (TEST), tuỳ coin. Với Sharpe cỡ đó, **lãi và sụt giảm tối đa đi cùng nhau theo tỉ lệ gần cố định**: muốn lãi gấp đôi thì phải chịu sụt giảm gần gấp đôi. Không có tham số nào tách hai thứ này ra. Mọi "mức lãi cao nhất" dưới đây đều là "cao nhất ở một mức sụt giảm chấp nhận được".

## 2. Bốn đòn bẩy đã đo

### 2a. Tăng cỡ vị thế (biến động mục tiêu, đòn bẩy tối đa)

Vốn 1000 USDT, TEST:

| | BNB vol 30% x2 | BNB vol 60% x2 | BNB vol 90% x4 | ETH vol 30% x2 | ETH vol 90% x4 |
|---|---|---|---|---|---|
| lãi/năm | +22,6% | +20,5% | +32,2% | +27,8% | +60,8% |
| sụt giảm tối đa | −41% | −64% | **−80%** | −36% | **−73%** |
| Sharpe | 0,85 | 0,62 | 0,78 | 1,00 | 0,97 |

Tăng cỡ vị thế hầu như **không tăng lãi ở TEST** (trừ ETH) nhưng đẩy sụt giảm lên 70–80%: một tài khoản 10 USDT sẽ chạm mức dừng 5 USDT gần như chắc chắn. Mức 30% của v2 là đúng. **Không tăng.**

### 2b. Gộp nhiều coin thành một danh mục (chia đều vốn)

Vốn đủ lớn, biến động 30%, TEST:

| danh mục | lãi/năm | sụt giảm | Sharpe |
|---|---|---|---|
| BNB một mình | +22,6% | −41% | 0,85 |
| ETH một mình | +27,8% | −36% | 1,00 |
| **BTC + ETH + BNB + XRP** | +19,2% | **−28%** | 0,85 |
| 8 coin (thêm ADA, DOGE, SOL, LINK) | +13,7% | −28% | 0,68 |
| 14 coin | +9,7% | −27% | 0,56 |

Đa dạng hoá **giảm sụt giảm** (−41% → −28%) nhưng thêm altcoin nhỏ **làm giảm lãi**, vì chúng tương quan cao với nhau và chiến lược theo xu hướng lỗ trên nhiều altcoin. Danh mục đáng dùng là **4 coin lớn**. Cần vốn tối thiểu (mục 3): khoảng **300 USDT** trở lên.

### 2c. Giảm cỡ vị thế khi đang sụt giảm (`FUTURES_DD_SCALE=0.3`: còn 25% cỡ khi sụt 30%)

Module thật, TEST:

| | không | có `DD_SCALE=0.3` |
|---|---|---|
| BNB, 1000 USDT | +23,3% / −40,6% | +11,2% / −23,7% |
| ETH, 1000 USDT | +27,8% / −35,7% | +15,8% / −21,7% |
| **BNB, 10 USDT** | +10,3% / −39,4% | **+9,8% / −39,4%** |
| 4 coin lớn (mô phỏng nhanh) | +19,2% / −28% | +10,7% / −20% |

Nó **cắt một nửa sụt giảm và cũng cắt một nửa lãi** (Sharpe không đổi hoặc giảm nhẹ): là núm chỉnh rủi ro, không phải cách tăng lãi. Với **10 USDT nó vô tác dụng**: lệnh tối thiểu của sàn đã ấn định cỡ vị thế, bot không thể giảm nhỏ hơn. Mặc định **tắt**; bật khi vốn lớn và bạn ưu tiên sụt giảm thấp.

### 2d. Bộ lọc BTC, cắt lỗ, mức dừng tổng (đã có trong v2)

Đây là các cơ chế chống cháy thật sự với tài khoản nhỏ: mức dừng 5 USDT đóng mọi thứ trước khi thanh lý; cắt lỗ 4 ATR giữ giá thanh lý ở xa; bộ lọc BTC tránh short altcoin khi BTC còn tăng. Trong 62 lần bắt đầu với 10 USDT từ 2020, BNB/ETH/DOGE **không lần nào** chạm mức dừng; không lần nào bị thanh lý (v1 không cắt lỗ bị thanh lý 5 lần với LTC).

## 3. Vốn tối thiểu để chiến lược chạy đúng cỡ

Chiến lược muốn vị thế = 30% ÷ biến động coin (nhân vốn). Vốn phải đủ để vị thế đó ≥ lệnh tối thiểu của sàn, nếu không lệnh bị nâng lên và **đòn bẩy thật cao hơn thiết kế**. Biến động 30 ngày trung vị của TEST:

| coin | lệnh tối thiểu | biến động | **vốn cần ≥** | lúc biến động cao (p90) |
|---|---|---|---|---|
| TRX | 5 USDT | 31% | 5 USDT | 10 |
| **BNB** | 5 | 46% | **8** | 12 |
| XRP | 5 | 66% | 11 | 19 |
| SOL | 5 | 72% | 12 | 17 |
| ADA | 5 | 77% | 13 | 18 |
| DOGE | 5 | 81% | 14 | 20 |
| LTC | 20 | 61% | 41 | 72 |
| ETH | 20 | 65% | 43 | 60 |
| LINK | 20 | 77% | 51 | 78 |
| BTC | 50 | 43% | 72 | 111 |

Với 10 USDT, chỉ **BNB** (và TRX) vừa. Đây là lý do ETH với 10 USDT ở TEST lỗ −14,5%/năm trong khi ETH với 1000 USDT lãi +27,8%.

## 4. Kết luận: cấu hình theo vốn

| vốn | cấu hình khuyến nghị | kỳ vọng hợp lý (theo TEST) |
|---|---|---|
| **10–40 USDT** | `FUTURES_SIGNAL_SYMBOLS=BNBUSDT`, v2, `DD_SCALE=0` (vô tác dụng ở mức này) | **≈ +10%/năm, sụt giảm 40%**: 10 USDT → khoảng 11 USDT sau một năm; mức dừng 5 USDT là chốt an toàn |
| 40–300 USDT | thêm ETH: `BNBUSDT,ETHUSDT` (từ ~90 USDT), chia đều | ≈ +15–25%/năm, sụt giảm 35–40% |
| ≥ 300 USDT | `BTCUSDT,ETHUSDT,BNBUSDT,XRPUSDT`; cân nhắc `DD_SCALE=0.3` | ≈ +19%/năm, sụt giảm 28%; với DD_SCALE: ≈ +11%, sụt giảm 20% |

**Mức lãi cao nhất "không cháy" mà dữ liệu ủng hộ là khoảng 20–28%/năm với sụt giảm 30–40%, và cần vốn vài trăm USDT để đạt được.** Với 10 USDT, trần thực tế là khoảng +1 USDT/năm. +100 USDT từ 10 USDT không nằm trong tầm của bất kỳ cấu hình nào ở đây mà không chấp nhận xác suất cháy trên 80% (`research/OPTIMIZE_RESULTS.md`, `README.md`). Cách duy nhất dữ liệu ủng hộ: **nạp thêm vốn** để chiến lược chạy đúng cỡ và đa dạng hoá được.

## 5. Những gì đã bị loại và vì sao

- Tăng đòn bẩy / biến động mục tiêu: sụt giảm 70–90% (mục 2a).
- Altcoin nhỏ và "xoay vòng altcoin mạnh nhất": lỗ (README, mục altcoin).
- Grid, martingale, breakout H1, Bollinger H1: lỗ trên mọi coin (`research/MT5_RESULTS.md`).
- Chọn coin theo thành tích quá khứ: xếp hạng không bền (`research/OPTIMIZE_RESULTS.md`).

## 6. Giới hạn của đánh giá này

TEST là 2,75 năm, một đường đi thị trường; v2 được chọn từ 2.592 bộ nên số TRAIN/VALID lạc quan; sổ ảo của bot dùng nến spot và funding giả định; không tính thuế, sự cố sàn, trượt giá lớn trong ngày sập. Hãy chạy sổ ảo vài tháng trước khi làm theo bằng tiền thật, và chỉ dùng tiền chấp nhận mất.
