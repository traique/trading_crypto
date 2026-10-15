# Các kiểu bot MT5 phổ biến trên futures Binance: kết quả kiểm tra

Tái lập: `python -m research.intraday_study [--grid-1m]` (tự tải nến 1 giờ, funding và nếu cần nến 1 phút từ kho Binance). Ngày chạy: 15/10/2026.

## Cách kiểm tra

- **7 kiểu EA** hay gặp trên MT5, dùng **tham số mặc định phổ biến, cố định trước khi chạy** (không tối ưu): breakout H1 (kênh 24 nến, cắt lỗ 1,5 ATR, chốt lời 2R), turtle H4 (vào ở kênh 20, ra ở kênh 10, cắt lỗ 2 ATR), MA cross H1 (EMA20/50, luôn có vị thế), RSI hồi quy H1 (RSI 30/70 theo xu hướng EMA200), Bollinger hồi quy H1 (dải 20,2), grid H1 (5 lệnh mua cách nhau 1%, chốt cả rổ ở +1%, cắt rổ ở −6%), martingale H1 (cùng lưới, khối lượng gấp đôi mỗi bậc). Chi tiết trong docstring của `research/intraday_study.py`.
- **Dữ liệu**: nến 1 giờ futures USDⓈ-M thật (BTC, ETH, SOL, BNB, XRP, DOGE), funding thật mỗi 8 giờ. TRAIN 2021–2023, **TEST 01/2024–09/2026**.
- **Chi phí và cơ chế**: phí 0,05% + trượt giá 0,05% mỗi lệnh thị trường; khớp ở giá mở cửa nến kế tiếp; nến chạm cả cắt lỗ lẫn chốt lời thì coi như cắt lỗ trước; giá mở cửa vượt qua cắt lỗ thì khớp ở giá mở cửa; thanh lý khi vốn chạm ký quỹ duy trì.
- **Quản lý vốn**: rủi ro 2% vốn mỗi lệnh, tối đa x3; lệnh dưới mức tối thiểu của sàn được nâng lên nếu ≤ x3, không thì bỏ. Grid/martingale tối đa x10.
- **Mục tiêu 10 → 110 USDT**: bắt đầu mỗi quý từ 2021 (19 lần), chạy tối đa 3 năm, dừng khi đạt 110 hoặc còn 5 USDT.

## Kết quả (TEST 2024–09/2026)

| kiểu EA | coin | vốn 10: lãi/năm, sụt giảm | vốn 1000: lãi/năm, sụt giảm | TRAIN (vốn 1000) | 10→110 trong 3 năm: đạt / chạm dừng |
|---|---|---|---|---|---|
| **turtle H4** | BTC | (không vào được lệnh) | +3%, −45% | +24% | 0 / 0 |
| | ETH | +46%, −49% | +25%, −33% | +4% | 0 / 0 |
| | SOL | −18%, −65% | +0%, −37% | +29% | 0 / 2 |
| | BNB | −10%, −56% | −13%, −58% | +45% | 0 / 2 |
| | XRP | +17%, −41% | +15%, −33% | −3% | 0 / 6 |
| | DOGE | +18%, −29% | +17%, −27% | +53% | 0 / 2 |
| **MA cross H1** | SOL | +13%, −52% | +17%, −55% | +104% | **6 / 0** |
| | DOGE | +75%, −63% | +74%, −63% | +20% | 0 / 5 |
| | BTC / ETH / BNB / XRP | lỗ | −10% đến −43% | lỗ | 0 / 0–19 |
| **RSI hồi quy H1** | 6 coin | −14% đến +8% | −9% đến +7% | ≈ 0 | 0 / 0 (ít lệnh) |
| **breakout H1** | 6 coin | −15% đến −49% | **−32% đến −77%** | −41% đến −61% | 0 / 19 (trừ BTC, ETH) |
| **Bollinger hồi quy H1** | 6 coin | −14% đến −49% | **−73% đến −87%** | −74% đến −85% | 0 / 19 |
| **grid H1** | 6 coin | −28% đến −71% | **−57% đến −95%** | −84% đến −93% | 0–1 / 15–19 |
| **martingale H1** | 6 coin | −28% đến −75% | **−60% đến −94%** | −87% đến −94% | 0–1 / 15–19 |

Bảng đầy đủ từng cặp: chạy lệnh tái lập ở trên.

### Grid và martingale: kiểm tra lại trên nến 1 phút

Kết quả của grid **phụ thuộc vào thứ tự giá bên trong nến**: mô phỏng trên nến 1 giờ mà giả định "xuống trước, lên sau" cho ra lãi hàng nghìn %/năm, còn giả định ngược lại cho ra lỗ nặng. Vì vậy grid và martingale được chạy lại trên **nến 1 phút** của TEST (`--grid-1m`):

| | ETH vốn 10 | ETH vốn 1000 | BTC vốn 10 | BTC vốn 1000 |
|---|---|---|---|---|
| grid | −45%/năm, sụt giảm −87% | −92%, −100% | −26%, −82% | −41%, −87% |
| martingale | −45%, −90% | −91%, −100% | −26%, −82% | −86%, −100% |

Tỉ lệ lệnh thắng **90–93%** nhưng hệ số lợi nhuận (PF) dưới 1: thắng nhỏ rất nhiều lần, rồi một lần cắt rổ (hoặc một đợt giảm sâu) xoá hết lãi. Đây chính là lý do backtest và tài khoản "signal" của loại EA này trên MQL5 thường rất đẹp trong vài tháng đầu rồi cháy. Kết quả nến 1 phút xác nhận cách mô phỏng thận trọng trên nến 1 giờ (nến nào khớp thêm bậc lưới thì không chốt lời trong chính nến đó).

## Kết luận

1. **Không kiểu EA nào đưa 10 USDT lên 110 USDT một cách đáng tin.** Trường hợp duy nhất có tỉ lệ đáng kể là MA cross H1 trên SOL (6/19), nhưng cùng quy tắc đó lỗ trên BTC, ETH, BNB và XRP. Chọn SOL sau khi đã xem bảng là chọn theo kết quả quá khứ.
2. **Grid, martingale, Bollinger và breakout H1 lỗ trên mọi coin, ở cả hai giai đoạn**: phí, trượt giá và nhiễu trong khung giờ ăn hết lợi thế. Đây là những loại EA được bán và quảng cáo nhiều nhất.
3. **Chỉ có dòng theo xu hướng là hoà vốn đến dương**, ở khung dài: turtle H4 trung bình khoảng +8%/năm trên TEST với vốn 1000 USDT (từ −13% đến +25%), +25%/năm trên TRAIN. Mức này ngang với chiến lược ngày `ls_sma` mà bot đang dùng, không tốt hơn rõ ràng. Muốn chạy turtle H4 thì bot còn phải chạy 4 giờ một lần thay vì một lần mỗi ngày.
4. Với vốn 10 USDT, BTC không vào được lệnh (tối thiểu 50 USDT). ETH chỉ vào được khi vốn ≥ 6,7 USDT, nên tài khoản ETH "đứng yên" ở khoảng 6 USDT chứ không chạm mức dừng 5.

**Quyết định:** không đưa EA nào ở trên vào bot. Bot giữ chiến lược ngày `ls_sma` (theo xu hướng, ít lệnh, có cắt lỗ và mức dừng tổng). Lý do: kiểu EA duy nhất không thua là cùng họ theo xu hướng với chiến lược này, và nó không cho kết quả tốt hơn.
