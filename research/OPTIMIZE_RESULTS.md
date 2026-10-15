# Tối ưu walk-forward chiến lược futures `ls_sma`

Tái lập: `python -m research.futures_study --symbols BTCUSDT` (tải dữ liệu nếu chưa có, cho từng coin trong `COINS`), rồi `python -m research.optimize_futures`. Ngày chạy: 15/10/2026. Bộ mô phỏng nhanh được test là cho cùng NAV từng ngày với `cryptotrader.futures.backtest` (`tests/test_research_optimize.py`).

## Quy trình (cố định trước khi chạy)

- Dữ liệu: nến ngày futures USDⓈ-M và funding thật của 14 coin niêm yết từ 2020: BTC, ETH, BNB, XRP, ADA, LINK, LTC, DOGE, TRX, DOT, SOL, AVAX, UNI, NEAR.
- Giai đoạn: TRAIN 20/07/2020–31/12/2022 (dò), VALID 2023 (chọn), TEST 01/2024–30/09/2026 (chạy một lần).
- Điểm: Sharpe ngày, lấy **trung vị qua 14 coin**, vốn 1000 USDT. Một bộ tham số cho mọi coin.
- Lưới 2.592 bộ: bộ SMA {50…200, 20…80} × long_min {5, 6, 7} × short_max {0, 1, 2} × biến động mục tiêu {0,3; 0,5; 0,8} × đòn bẩy tối đa {1, 2, 3} × cắt lỗ ATR {không, 2, 3, 4} × biên {0,1; 0,25} × lọc BTC {tắt, bật}.
- Chọn: 20 bộ tốt nhất ở TRAIN, rồi bộ có VALID tốt nhất trong số đó (VALID > 0, sụt giảm trung vị TRAIN tốt hơn −70%).

## Kết quả

Bộ được chọn: SMA 20…80, long ≥ 6/7, short ≤ 2/7, biến động 30%, x2, **không cắt lỗ**, biên 0,25, **lọc BTC bật**. Trong top 20, gần như tất cả đều dùng SMA nhanh, biến động 30% và lọc BTC. Đòn bẩy tối đa và cắt lỗ 0/2/4 ATR hầu như không đổi điểm, nghĩa là kết quả nằm trên một "vùng phẳng", không phải một điểm nhọn may mắn.

| trung vị 14 coin, vốn 1000 USDT | TRAIN Sharpe / lãi / sụt giảm | VALID Sharpe / lãi | TEST Sharpe / lãi / sụt giảm |
|---|---|---|---|
| v1 (bản đầu) | 0,45 / +10,5% / −59% | 0,19 / −4,4% | 0,32 / +3,4% / −55% |
| bộ được chọn (không cắt lỗ) | 0,96 / +29,5% / −26% | 1,26 / +35,5% | 0,36 / +7,2% / −35% |
| **v2 = bộ được chọn + cắt lỗ 4 ATR** | 0,92 / +28,3% / −26% | 1,26 / +35,4% | 0,35 / +7,4% / −35% |

TEST theo từng coin (v2, vốn 1000 USDT, lãi năm): BTC +10%, ETH +28%, BNB +23%, XRP +10%, ADA −1%, LINK +0%, LTC −1%, DOGE +26%, TRX +7%, DOT +2%, SOL +4%, AVAX +8%, UNI +8%, NEAR −12%.

**Vì sao thêm cắt lỗ 4 ATR:** với 10 USDT, lệnh tối thiểu có thể ép đòn bẩy lên x2–3. Khi đó bản không cắt lỗ bị thanh lý sạch tài khoản ở 5/62 lần bắt đầu với LTC. Bản 4 ATR có điểm TRAIN/VALID/TEST gần như bằng nhau và không bị thanh lý với LTC. UNI vẫn bị thanh lý 6 lần ở cả hai bản (khối lượng tối thiểu 1 UNI), nên đừng chạy UNI với 10 USDT.

**Xếp hạng coin không bền.** Tổng Sharpe TRAIN+VALID cao nhất thuộc về AVAX (4,1), SOL (3,8), NEAR (3,5), nhưng TEST của chúng là +8%, +4%, −12%. ETH và DOGE ở giữa bảng lại tốt nhất ở TEST. Không chọn coin theo thành tích quá khứ.

**Vốn 10 USDT.** Bắt đầu với 10 USDT từ 01/2024 (module thật, `research.futures_study --preset v2`):

| coin | lãi năm | sụt giảm | 10 → 110 USDT trong 3 năm (62 lần): đạt / chạm 5 USDT |
|---|---|---|---|
| ETH | −14,5% | −71% | 0 / 0 |
| BNB | +10,3% | −39% | 0 / 0 |
| DOGE | +32,4% | −32% | 0 / 0 |
| XRP | +10,4% | −40% | 0 / 6 |
| SOL | −0,6% | −68% | 0 / 6 |

ETH ở 10 USDT thua xa ETH ở 1000 USDT (+27,8%), vì lệnh tối thiểu 20 USDT ép đòn bẩy gấp 5–7 lần mức chiến lược muốn. Tín hiệu thật mặc định là BNB theo tiêu chí cấu trúc, không theo TEST (xem README).

## Giới hạn

- TEST chỉ là 2,75 năm, một đường đi của thị trường. Phần chênh v2 so với v1 ở TEST (Sharpe 0,35 so với 0,32) là nhỏ; lợi thế rõ là sụt giảm thấp hơn.
- 2.592 bộ tham số đã thử nên số TRAIN/VALID lạc quan. Số TEST là ước lượng tốt nhất, không phải bảo đảm.
- Nến ngày, khớp ở giá mở cửa; sổ ảo của bot dùng nến spot và funding giả định.
