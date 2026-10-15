# Bộ khung backtest nghiên cứu (`research/`)

Bộ khung này dùng để thử chiến lược trên **toàn bộ cặp USDT giao ngay của Binance từng được niêm yết**, kể cả các cặp đã huỷ niêm yết, rồi so sánh sau phí với BTC mua và giữ. Nó được mô phỏng theo bộ khung của dự án cổ phiếu Việt Nam, nhưng các quy tắc là của Binance spot.

- **Point-in-time:** quyết định ở giá đóng cửa ngày D (00:00 UTC, tức 07:00 giờ Việt Nam) chỉ thấy nến tới hết ngày D. Rổ cặp giao dịch được dựng từ dữ liệu **trước** ngày giao dịch, gồm cả các cặp sau này bị huỷ niêm yết.
- **Đúng sổ lệnh của bot:** mọi lệnh đi qua `cryptotrader.trading.ledger.Ledger` thật, tức là đúng code mà tài khoản giấy (paper) của bot chạy:
  - khớp ở giá mở cửa ngày sau;
  - trượt giá 0,1% mỗi chiều, làm tròn theo bước giá về phía bất lợi;
  - phí taker 0,1% mỗi lần khớp, tính bằng USDT;
  - khối lượng lẻ, làm tròn theo `stepSize`, có kiểm tra `minQty` và `minNotional`;
  - không có thời gian chờ thanh toán.
- **Giai đoạn cố định, TEST bị khoá:** xem [phần dưới](#giai-đoạn-và-khoá-test).

Gói `research/` nằm ngoài `cryptotrader/`. Bot không cần nó để chạy, và nó không được đưa vào gói Botkeep.

## Tải dữ liệu

```bash
pip install -e ".[dev]"
python -m research download              # lần đầu ~30 phút (22 phút với --workers 12); các lần sau chỉ tải phần mới
```

Mọi lệnh `python -m research ...` chạy từ thư mục gốc của repo.

- **Vị trí:** mặc định `research/data/`. Đặt biến môi trường `CRYPTO_RESEARCH_DATA=/đường/dẫn` để dùng thư mục khác. Thư mục dữ liệu và `research/out/` đã nằm trong `.gitignore`: **không bao giờ commit dữ liệu**.
- **Dung lượng:** khoảng 70 MB CSV cho ~760 cặp. Lần chạy backtest đầu tiên dựng thêm cache `research_panel.pkl` (~150 MB, vài giây).
- **Nguồn:** kho lưu trữ công khai của Binance (`data.binance.vision`). Đây là nguồn duy nhất còn giữ nến của **mọi** cặp từng giao dịch: REST API quên một cặp ngay khi nó bị huỷ niêm yết.
  1. Danh sách cặp: mọi thư mục `*USDT` trong `data/spot/monthly/klines/` và `daily/klines/`, lấy từ danh sách S3 công khai, lưu vào `archive_symbols.json`.
  2. Nến ngày của từng cặp: các tệp zip theo tháng, rồi tệp theo ngày cho phần sau tháng cuối cùng. Kết quả lưu vào `klines/{CẶP}.csv`.
  3. Trạng thái hôm nay của từng cặp (exchangeInfo, `data-api.binance.vision`) lưu vào `exchange_status.json`, chỉ để báo cáo độ phủ. Backtest không đọc tệp này.
- **Kiểm tra toàn vẹn:** mỗi tệp zip được so với MD5 mà danh sách S3 công bố (ETag). Với `--sha256`, nó còn được so với tệp `.CHECKSUM` SHA-256 chính thức (gấp đôi số yêu cầu). Tệp sai checksum làm cặp đó lỗi; không có gì chưa kiểm tra được ghi xuống đĩa.
- **Tải tiếp và cập nhật:** chạy lại cùng lệnh.
  - `klines/{CẶP}.json` ghi lại mọi tệp đã gộp cùng ETag của nó. Lần chạy sau chỉ tải tệp mới, hoặc tệp được Binance đăng lại với ETag khác.
  - Tệp theo ngày bị thay bằng tệp tháng khi tệp tháng xuất hiện.
  - Mỗi tệp được ghi vào tệp tạm rồi đổi tên, nên tiến trình bị tắt giữa chừng không để lại tệp ghi dở.
  - **Ctrl-C:** huỷ các cặp còn trong hàng đợi, chờ các cặp đang tải xong, ghi `download_meta.json` rồi thoát với mã 130. Chạy lại để tải tiếp.
  - **Ngày cuối dữ liệu** (`data_end`) chỉ tiến lên sau một lần tải **đầy đủ**. Lần chạy với `--symbols`/`--limit` hoặc bị ngắt giữ nguyên `data_end`; nếu không, các cặp không được cập nhật sẽ trông như đã ngừng giao dịch.
- **Lịch sự với nguồn:** mặc định 8 luồng (tối đa 16, `--workers`). Mỗi yêu cầu thử lại tối đa 4 lần, chờ tăng dần, và tôn trọng `Retry-After` khi bị giới hạn tốc độ.
- **Lỗi:** ghi vào `download.log` và vào trường `"failed"` của `download_meta.json`. Chạy lại lệnh để thử lại.
- **`download_meta.json`** ghi cho từng cặp:
  - ngày giao dịch đầu và cuối (nến có khối lượng);
  - số ngày giao dịch;
  - số "đoạn niêm yết": một khoảng trống hơn 7 ngày nghĩa là đã huỷ rồi niêm yết lại;
  - còn giao dịch ở ngày cuối dữ liệu hay không;
  - lỗi, nếu có.
- **Tuỳ chọn khác:** `--refresh` tải lại toàn bộ; `--symbols BTCUSDT,ETHUSDT` chỉ tải vài cặp; `--limit N` chỉ tải N cặp đầu để thử.

### Độ phủ dữ liệu

Bộ dữ liệu tải ngày 15/10/2026 (tới nến 14/10/2026), bằng `python -m research download --workers 12`, mất 22 phút và chiếm 64 MB:

- **Danh sách:** kho lưu trữ liệt kê **764 cặp USDT**. Tải được **763 cặp**; `USDSOLDUSDT` (stablecoin cũ) chỉ có nến tuần/tháng, không có nến ngày, nên luôn nằm trong `"failed"`.
- **Còn giao dịch hôm nay: 509 cặp**, khớp đúng số cặp USDT có trạng thái `TRADING` trong exchangeInfo.
- **Đã ngừng giao dịch: 254 cặp.** Bỏ token đòn bẩy và stablecoin thì còn **192 cặp coin thật** đã ngừng: LUNA cũ, FTT cũ, SRM, BTT cũ, NPXS, ERD, BCC, BCHABC...
  - Theo năm ngừng: 2018: 2, 2019: 2, 2020: 6, 2021: 3, 2022: 24, 2023: 14, 2024: 40, 2025: 51, 2026: 50.
  - Một phần là **đổi tên hoặc chuyển đổi token**, không phải bị loại: BCC→BCHABC (2018), ERD→EGLD (2020), MATIC→POL (2024), RNDR→RENDER (2024), FTM→S (2025)... Bộ khung coi chúng như huỷ niêm yết: bán ở giá đóng cửa cuối.
- **Niêm yết lại: 16 cặp** (khoảng trống hơn 7 ngày). Ví dụ LUNA (cũ, rồi Terra 2.0), FTT, KEY, VIDT, STRAX.
- **Bắt đầu:** BTCUSDT và ETHUSDT từ 17/08/2017. Số cặp coin thật mới niêm yết theo năm: 2017: 6, 2018: 15, 2019: 65, 2020: 103, 2021: 123, 2022: 41, 2023: 52, 2024: 61, 2025: 98, 2026: 119.
- **Panel đã khoá (tới 31/12/2023):** 2.328 ngày × 476 cặp (405 cặp giao dịch được sau quy tắc loại trừ), trong đó 105 cặp đã ngừng trước cuối 2023.
- **Cặp ngừng giao dịch trong rổ:**
  - `liq_top20`: 102 cặp từng vào rổ trong 2018–2023, 24 cặp trong số đó nay không còn giao dịch.
  - `liq_top50`: 209 cặp từng vào rổ, 74 cặp nay không còn giao dịch.
  - Một rổ dựng từ danh sách cặp **hôm nay** sẽ thiếu chúng: đó là thiên lệch sống sót mà bộ khung này tránh được.

## Dòng lệnh

```bash
python -m research list                                    # các chiến lược có sẵn
python -m research run btc_sma_ens --split VALID           # chiến lược của bot (module live, chạy y như bot)
python -m research run btc_sma_ens_eval --split VALID      # cùng quy tắc, từ code đã kiểm định (đóng băng)
python -m research run btc_trend --split TRAIN             # chiến lược giữ chỗ SMA200 cũ (mốc so sánh)
python -m research run xs_momentum --split VALID --universe liq_top30 --slippage-mult 2
python -m research run ew --universe liq_top10 --start 2019-01-01 --end 2020-12-31 --out research/out/ew_2019
python -m research run price:BTCUSDT --split VALID         # giá BTC, không phí (thước đo)
python -m research baselines --out research/out            # bảng baseline -> baselines.{json,md}
python -m research run btc_trend --split TEST --unlock-test   # chỉ người kiểm định (đã chạy một lần ngày 15/10/2026)
```

`run <chiến lược>`:

| chiến lược | là gì |
|---|---|
| `btc_sma_ens` | `cryptotrader.strategies.BtcSmaEnsemble`, chiến lược của bot (`C2_btc_sma_ens` đã kiểm định): tỷ trọng BTC = số đường SMA 50..200 ngày mà giá nằm trên, chia 7; biên 10%. Chạy qua `StrategyContext` y như bot |
| `btc_sma_ens_eval` | cùng quy tắc, từ bản sao **đóng băng, giống từng byte** của code người kiểm định đã chạy (`research/evaluated/`); dùng để đối chiếu từng lệnh khớp với `btc_sma_ens` |
| `btc_buy_hold` | `cryptotrader.strategies.BtcBuyHold` (100% BTC), chạy qua `StrategyContext` y như bot, có phí |
| `btc_trend` | `cryptotrader.strategies.BtcTrend`: giữ BTC khi giá đóng cửa trên SMA200, nếu không thì giữ USDT. Chiến lược giữ chỗ cũ, nay là mốc so sánh; trượt cổng TRAIN (MDD −64,7%) và Sharpe TEST thấp hơn BTC |
| `ew` | chia đều cho cả rổ `--universe`, cân bằng lại cuối mỗi tháng |
| `xs_momentum` | động lượng chéo ngây thơ: 5 cặp có lợi nhuận 90 ngày cao nhất trong rổ (mặc định `liq_top20`), chia đều, cân bằng lại hằng tháng |
| `buyhold:CẶP` | mua một cặp ở ngày đầu và giữ, có phí |
| `price:CẶP` | giá của một cặp làm NAV: không phí, không sổ lệnh |

Tuỳ chọn:

- `--split TRAIN|VALID|TRAIN+VALID|TEST|ALL` (mặc định VALID), hoặc `--start/--end YYYY-MM-DD`.
- `--unlock-test`: mở khoá TEST (xem dưới).
- `--capital 10000`: vốn ban đầu, tính bằng USDT.
- `--slippage-mult 2`: nhân trượt giá lên, để thử độ bền với chi phí.
- `--fee-pct 0.075`: phí mỗi lần khớp, tính theo %. Mặc định 0,1; mức 0,075 là khi trả phí bằng BNB.
- `--max-adv-pct 0.01`: mỗi lệnh mua tối đa 1% khối lượng USDT trung bình 30 ngày.
- `--universe liq_top10|liq_top20|liq_top30|liq_top50` (mặc định `liq_top20`).
- `--out THƯ_MỤC`: ghi các tệp sau:
  - `summary.json`: chỉ số, BTC, từng năm, tuỳ chọn, cảnh báo;
  - `nav.csv`: NAV, tiền, phần đang đầu tư, giá BTC theo ngày;
  - `fills.csv`: mọi lệnh khớp (giá, phí, lãi/lỗ);
  - `yearly.csv`.

Kết quả in ra gồm bảng chỉ số của chiến lược và của BTC mua và giữ cùng giai đoạn (**không phí**), rồi lợi nhuận từng năm kèm cột BTC và phần vượt trội.

Ý nghĩa các chỉ số:

- **CAGR:** theo số ngày lịch / 365,25.
- **Sharpe:** theo ngày, lãi suất phi rủi ro bằng 0, nhân √365 (thị trường chạy 365 ngày/năm).
- **Sortino:** lợi nhuận trung bình chia độ lệch giảm, nhân √365.
- **Calmar:** CAGR chia |sụt giảm tối đa|.
- **Turnover:** một chiều, (mua + bán) / 2 / NAV bình quân / năm.
- **Time invested:** tỷ lệ ngày có hơn 5% NAV ở coin.
- **Fees:** tổng phí đã trả, tính bằng USDT và theo % vốn ban đầu. Trượt giá đã nằm trong giá khớp, không cộng vào phí.
- **NAV:** định giá theo giá đóng cửa; không trừ chi phí thanh lý cuối kỳ.
- **Tiền mặt (USDT):** không có lãi. Không tính lãi Earn/staking.

## Viết một chiến lược

Có hai cách.

**1. Chiến lược của bot** (`cryptotrader.strategies`): một object có `name`, `symbols`, `warmup_days` và `decide(ctx: StrategyContext)`. Bộ khung dựng `StrategyContext` y như bot: nến của `symbols` tới hết ngày D, tỷ trọng hiện tại, NAV, tiền, `state`. Kết quả đi qua `ledger.plan_target_weights`. Đăng ký nó trong `cryptotrader/strategies/__init__.py` (`REGISTRY`) để bot dùng được.

**2. Chiến lược nghiên cứu:** một object có `on_day(ctx)` (hoặc chỉ là một hàm `f(ctx)`). Hàm này được gọi ở giá đóng cửa mỗi ngày D và trả về lệnh cho ngày D+1:

- `None`: không làm gì;
- `dict {cặp: tỷ trọng}`: tỷ trọng mục tiêu theo NAV, phần còn lại giữ USDT.
  - Sổ lệnh tự tính lệnh qua `plan_target_weights`: bán trước, mua sau.
  - Bỏ qua thay đổi nhỏ hơn 1% NAV; đặt thuộc tính `min_trade_pct` trên chiến lược để đổi ngưỡng này.
  - Tổng tỷ trọng trên 1 được thu nhỏ lại. Không bán khống.
- `list` lệnh (`ctx.buy(...)`, `ctx.sell(...)`): dùng cho lệnh giới hạn, stop, mục tiêu, trailing stop.

```python
from research.backtest import run
from research.config import TRAIN, VALID


class TopKMomentum:
    name = "topk_mom"

    def __init__(self, k=5, lookback=60, ma=100):
        self.k, self.lookback, self.ma = k, lookback, ma

    def on_day(self, ctx):
        btc = ctx.btc                              # chỉ tới hôm nay
        if len(btc) < self.ma or btc.iloc[-1] < btc.iloc[-self.ma:].mean():
            return {}                              # rổ rỗng -> bán hết, giữ USDT
        if ctx.date.dayofweek != 6 and ctx.positions:
            return None                            # chỉ cân bằng lại mỗi Chủ nhật
        syms = ctx.universe("liq_top30")           # rổ point-in-time cho ngày mai
        px = ctx.history("close", syms, self.lookback + 1)
        ret = (px.iloc[-1] / px.iloc[0] - 1).dropna()
        top = ret[ret > 0].nlargest(self.k).index
        return dict.fromkeys(top, 1.0 / self.k)


res = run(TopKMomentum(), *TRAIN)                  # chỉnh tham số trên TRAIN
print(res.summary())
print(res.yearly)
print(run(TopKMomentum(), *VALID).summary())       # rồi kiểm tra trên VALID
```

Trên dữ liệu tải ngày 15/10/2026, ví dụ này cho TRAIN **+1.112%** (CAGR 86,6%, MDD −66%) nhưng VALID **−50,3%** (BTC −11,4%). Một con số TRAIN đẹp không nói gì về tương lai.

Muốn gọi chiến lược mới bằng tên trên dòng lệnh, thêm nó vào `STRATEGIES` / `make()` trong `research/strategies.py`.

Những gì `ctx` cho xem, tất cả cắt tới ngày D:

- Khung giá rộng (ngày × cặp): `ctx.close / open / high / low / volume / quote_volume / trades`.
  - `quote_volume`: khối lượng tính bằng USDT.
  - Ô NaN là ngày cặp đó không giao dịch.
- Chuỗi giá: `ctx.btc` (giá đóng cửa BTCUSDT), `ctx.candles(cặp)` (nến dạng `cryptotrader.data.klines`, như bot tải).
- Hàm tiện ích:
  - `ctx.history(field, symbols, n)`;
  - `ctx.universe(kind)`: rổ cho ngày mai;
  - `ctx.last_close(cặp)`: `None` nếu hôm nay cặp đó không có nến;
  - `ctx.adv(cặp)`: khối lượng USDT trung bình 30 ngày.
- Sổ lệnh: `ctx.ledger`, `ctx.nav`, `ctx.cash`, `ctx.positions`, `ctx.weights()`, `ctx.rebalance(weights)`.
- Bộ nhớ riêng của chiến lược trong suốt lần chạy: `ctx.state`.
- **Không đọc `ctx._panel`**: đó là toàn bộ dữ liệu, kể cả tương lai.

Rổ (`research.universe`), dựng từ dữ liệu **trước** ngày giao dịch: `liq_topN` là N cặp có trung vị khối lượng USDT 30 ngày cao nhất (ngày không giao dịch tính bằng 0). Điều kiện để vào rổ:

- là cặp USDT giao dịch được theo `cryptotrader.market.rules.exclusion_reason`: loại stablecoin và tiền pháp định, token đòn bẩy (`BTCUP`/`BTCDOWN`...), token bọc hoặc neo giá (WBTC, PAXG...);
- có ít nhất 180 ngày giao dịch trong **đoạn niêm yết hiện tại**: một cặp bị huỷ rồi niêm yết lại (khoảng trống hơn 7 ngày) phải đếm lại từ đầu;
- có nến trong 3 ngày gần nhất.

`run(strategy, start, end, *, capital=10_000, universe_kind="liq_top20", slippage_mult=1.0, fee_pct=None, max_adv_pct=None, unlock_test=False)` trả về `Result` gồm:

- `.metrics` và `.benchmark` (BTC mua và giữ cùng giai đoạn, theo giá, không phí);
- `.yearly`, `.daily`, `.fills`, `.ledger`;
- `.warnings` (ví dụ: cặp bị thanh lý vì ngừng giao dịch);
- `.options`, `.summary()`.

Phí và trượt giá được đặt trên chính object `Ledger` của lần chạy (không sửa biến toàn cục), nên chạy song song bằng thread hay process đều được.

## Giai đoạn và khoá TEST

| giai đoạn | ngày | dùng để |
|---|---|---|
| TRAIN | 01/01/2018 – 31/12/2021 | tìm ý tưởng, chỉnh tham số |
| VALID | 01/01/2022 – 31/12/2023 | kiểm tra ngoài mẫu, so sánh ứng viên |
| TEST | 01/01/2024 – nay | **người kiểm định chạy một lần**, sau khi đã chốt quy tắc chọn (đã chạy 15/10/2026, kết quả trong [`RESULTS.md`](RESULTS.md)) |

`run()` từ chối mọi giai đoạn kết thúc sau 31/12/2023 (`TestPeriodLocked`) nếu không có `unlock_test=True`. Nó cũng từ chối một panel có chứa ngày TEST.

`load_panel()` mặc định trả về một bản **cắt** ở 31/12/2023. Nhưng đó không phải bức tường: panel đầy đủ vẫn nằm trong bộ nhớ (`research.data._FULL`) và trong cache `research_panel.pkl`, và các tệp CSV trong `klines/` có cả ngày TEST. Code nghiên cứu **không được** đọc chúng. Dòng lệnh cần `--unlock-test` cho `--split TEST`, `ALL` hoặc `--end` sau 2023.

**Vì sao phải khoá: chống overfitting.** Mỗi lần nhìn kết quả rồi sửa chiến lược là một lần "học" từ dữ liệu đó; sau vài trăm lần thử, con số đẹp nhất phần lớn là may mắn. TEST chỉ là phép thử sạch nếu:

1. quy tắc chọn được viết ra **trước** khi chạy TEST;
2. TEST chỉ chạy **một lần**, cho các ứng viên đã chốt;
3. không quay lại sửa chiến lược vì kết quả TEST. Nếu đã sửa, phải đợi dữ liệu mới làm TEST mới.

Thói quen tốt:

- chỉnh tham số trên TRAIN, xem VALID;
- đọc `res.yearly`: có thắng BTC ở đa số các năm không?;
- thử tham số lân cận (±1 bước không được lật kết quả);
- thử `--slippage-mult 2`, `--max-adv-pct 0.01` với vốn lớn, rổ `liq_top30/50`, dời ngày bắt đầu 1–3 tháng;
- luôn so với `btc_buy_hold`, `ew` và `xs_momentum` cùng giai đoạn.

## Baseline (TRAIN, VALID, TRAIN+VALID)

Chạy bằng `python -m research baselines` (dữ liệu tới 31/12/2023, TEST bị khoá). Điều kiện: vốn 10.000 USDT, phí 0,1% mỗi lần khớp, trượt giá 0,1%, khớp ở giá mở cửa ngày sau. Bảng đầy đủ (Sortino, Calmar, biến động, vòng quay, phí, các phép thử với trượt giá ×2, rổ `liq_top50`, vốn 1 triệu USDT với giới hạn 1% khối lượng) nằm trong `baselines.md`, ghi ra thư mục `--out`.

| chiến lược | TRAIN 2018–2021: tổng % / MDD % / Sharpe | VALID 2022–2023: tổng % / MDD % / Sharpe | TRAIN+VALID: CAGR % / MDD % |
|---|---|---|---|
| giá BTC (không phí) | +245,4 / −81,2 / 0,80 | −11,4 / −66,9 / 0,17 | 21,1 / −81,2 |
| `btc_buy_hold` | +244,7 / −81,2 / 0,80 | −11,6 / −67,0 / 0,16 | 21,1 / −81,2 |
| ETH mua và giữ | +385,9 / −94,0 / 0,91 | −39,5 / −74,0 / −0,01 | 20,2 / −94,0 |
| `ew` chia đều `liq_top10`, tháng | +157,1 / −83,8 / 0,72 | −66,2 / −84,0 / −0,37 | −1,8 / −89,5 |
| `btc_trend` (SMA200, giữ chỗ) | +114,8 / −64,7 / 0,63 | +93,3 / −18,9 / 1,27 | 26,8 / −64,7 |
| `xs_momentum` top 5 `liq_top20` theo lợi nhuận 90 ngày, tháng | −22,2 / −88,7 / 0,43 | −80,2 / −93,3 / −0,42 | −25,9 / −94,8 |

Đọc bảng này thế nào:

- **Đây là số trong mẫu, không phải lợi nhuận kỳ vọng.** TRAIN và VALID đã được nhìn khi viết bảng này.
- **Không baseline nào thắng BTC mua và giữ trên TRAIN.** Rổ altcoin chia đều và động lượng chéo ngây thơ thua xa BTC trên cả hai giai đoạn. Một phần lớn là vì dữ liệu có cả các cặp đã chết: LUNA về 0 trong rổ tháng 5/2022.
- **`btc_trend` chỉ thắng nhờ đứng ngoài cả năm 2022** (0% so với −64% của BTC). Trên TRAIN nó thua BTC: các lần cắt ra vào quanh SMA200 năm 2018 và 2021 tốn 37 lệnh và 7,7% vốn phí trên TRAIN+VALID. Tham số 200 ngày là quy ước phổ biến, không được chỉnh trên dữ liệu này. Nhưng một chiến lược thắng nhờ một năm duy nhất là bằng chứng yếu; phải chờ TEST. (TEST đã chạy: 19,7% / −32,3% / Sharpe 0,68, thấp hơn Sharpe 0,70 của BTC; xem [`RESULTS.md`](RESULTS.md).)
- **Trượt giá ×2** chỉ làm `ew` mất thêm ~0,4 điểm CAGR/năm và `xs_momentum` ~0,9 điểm: vòng quay 3–6 lần/năm nên chi phí không phải vấn đề chính ở vốn nhỏ.
- **Vốn 1 triệu USDT với giới hạn 1% khối lượng 30 ngày:** `xs_momentum` gần như không đổi, vì rổ `liq_top20` đủ thanh khoản.
- **Đầu TRAIN:** các chiến lược theo rổ giữ USDT tới khoảng tháng 2/2018, và rổ chỉ có BTC và ETH tới đầu tháng 5/2018 (điều kiện 180 ngày).

## Những điều cần biết trước khi tin một con số

1. **Con số trong mẫu không phải lợi nhuận kỳ vọng.** Mọi bảng ở đây là TRAIN/VALID: dữ liệu đã được nhìn. Chỉ TEST, chạy một lần, mới là ước lượng ngoài mẫu.
2. **Thiên lệch sống sót đã giảm nhiều, chưa hết.**
   - Kho lưu trữ giữ cả cặp đã huỷ niêm yết (LUNA cũ, FTT, SRM, BTT cũ...), nên rổ point-in-time gồm cả chúng khi chúng còn giao dịch.
   - Vẫn thiếu: cặp chỉ giao dịch với BTC/BNB/BUSD mà chưa bao giờ có cặp USDT; các sàn khác.
   - Mã được tái sử dụng cho tài sản khác (ví dụ LUNA cũ và LUNA mới) được tách bằng quy tắc "khoảng trống hơn 7 ngày". Một lần đổi tài sản không để lại khoảng trống sẽ không bị phát hiện.
3. **Cặp ngừng giao dịch** (huỷ niêm yết, hoặc tạm dừng hơn 7 ngày) mà đang giữ thì bị **thanh lý ở giá đóng cửa cuối cùng**, trừ trượt giá và phí, vào ngày thứ 8 không có nến. Cảnh báo nằm trong `Result.warnings`. Không có nhìn trước: khoảng trống phải xảy ra rồi mới biết; trong thời gian đó vị thế vẫn được định giá ở giá cuối và tiền chưa về.
   - Đây là giả định **lạc quan**: thực tế phải bán trên sàn trong tuần báo trước, thanh khoản mỏng, hoặc bị kẹt. Nhưng cú sập sau thông báo huỷ niêm yết đã nằm trong nến mà chiến lược phải giữ qua.
   - Vị thế dưới mức tối thiểu của sàn bị xoá sổ (về 0).
4. **Bộ lọc sàn:** tickSize/stepSize lịch sử không còn. Sổ lệnh dùng `rules.DEFAULT_FILTERS` (lưới giá và khối lượng mịn, `minNotional` 10 USDT) cho mọi cặp và mọi năm.
5. **Khớp ở giá mở cửa ngày sau** cộng 0,1% trượt giá, cả lệnh, bất kể khối lượng, trừ khi đặt `--max-adv-pct`.
   - Ở 10.000 USDT với `liq_top10/20` thì ổn.
   - Với cặp nhỏ hoặc vốn lớn nên đặt giới hạn này. Giới hạn chỉ áp cho lệnh mua; lệnh bán được giả định khớp đủ.
6. **Phí:** 0,1% taker mỗi lần khớp, không giảm giá BNB/VIP. Không có phí rút, không có funding (chỉ giao ngay).
7. **Nến không có khối lượng** (Binance vẫn in nến phẳng cho một số cặp đang tạm dừng hoặc đã huỷ) được coi là ngày không giao dịch.
8. **Đầu TRAIN gần như không có rổ.** Cặp USDT trên Binance bắt đầu từ 17/08/2017, nên điều kiện 180 ngày làm rổ `liq_topN` rỗng cho tới khoảng tháng 2/2018 và chỉ có vài cặp tới giữa 2018. Chiến lược theo rổ vì vậy giữ USDT ở đầu TRAIN; xem cột "time inv %".
9. **Múi giờ:** ngày là ngày UTC. Nến ngày đóng lúc 00:00 UTC (07:00 giờ Việt Nam) và được coi là chốt từ 00:05 UTC (`rules.final_day`).

## Kiểm thử

```bash
python -m pytest -q tests/test_research_harness.py                  # dữ liệu giả lập, không cần mạng
CRYPTO_RESEARCH_DATA=/đường/dẫn python -m pytest -q tests/test_research_harness.py -m integration   # dữ liệu thật + mạng
```

Test trên dữ liệu giả lập:

- mua và giữ khớp đúng lợi nhuận giá trừ phí và trượt giá, tới từng phần tỷ;
- chiến lược của bot (`BtcBuyHold`) cho cùng sổ lệnh;
- `ctx` không bao giờ có dòng sau ngày D;
- đổi dữ liệu tương lai không đổi NAV quá khứ hay rổ quá khứ;
- khoá TEST trong `run()`, `load_panel()` và dòng lệnh;
- thanh lý cặp ngừng giao dịch ở giá cuối, nhưng không thanh lý khi chỉ tạm dừng ngắn;
- nến không có khối lượng;
- giới hạn thanh khoản;
- quy tắc rổ: loại trừ, 180 ngày, đoạn niêm yết lại, chỉ dùng dữ liệu trước ngày;
- bộ tải:
  - tải tăng dần, tệp tháng thay tệp ngày;
  - từ chối tệp sai checksum, không ghi gì;
  - không dò tệp ngày cho cặp đã huỷ lâu;
  - phân trang S3;
  - `data_end` chỉ dời sau lần tải đầy đủ;
  - quên mã gõ sai.

Test `integration` dùng dữ liệu tại `CRYPTO_RESEARCH_DATA` (bỏ qua nếu chưa tải) và mạng thật:

- BTC mua và giữ năm 2020 so với giá;
- panel bị khoá và có hơn 100 cặp đã ngừng giao dịch;
- rổ point-in-time (LUNA có trong rổ tháng 5/2022, mất khỏi rổ sau khi sập);
- tải thật một cặp đã huỷ niêm yết (SRMUSDT) từ kho lưu trữ.

## Tệp

| tệp | nội dung |
|---|---|
| `config.py` | đường dẫn (`CRYPTO_RESEARCH_DATA`), các giai đoạn, ngày khoá, ngưỡng huỷ niêm yết |
| `download.py` | tải và cập nhật dữ liệu từ kho lưu trữ Binance |
| `data.py` | `Panel`, `make_panel`, `load_panel` (cache pickle, bản cắt khoá TEST) |
| `universe.py` | rổ `liq_topN` point-in-time |
| `backtest.py` | `run`, `run_price`, `Context`, `Result`, thanh lý cặp ngừng giao dịch |
| `metrics.py` | chỉ số (365 ngày/năm) |
| `strategies.py` | `BuyHold`, `EqualWeightMonthly`, `CrossSectionalMomentum`, chiến lược của bot theo tên |
| `baselines.py` | bảng baseline |
| `cli.py`, `__main__.py` | dòng lệnh |
