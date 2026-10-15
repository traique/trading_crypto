# trading-crypto

Nghiên cứu và giao dịch tài khoản ảo / tín hiệu cho **Binance spot, các cặp USDT**. Dự án tái sử dụng phần khung đã được gia cố của TradingAgents VN (agent LangGraph, bot web + Telegram, sổ giao dịch, bộ backtest có giai đoạn TEST khoá), nhưng chỉ áp dụng cho crypto.

Một tiến trình `main_botkeep.py` chạy liên tục gồm:

- **Bot giao dịch hằng ngày** sau khi nến ngày Binance đóng (00:00 UTC = 07:00 giờ Việt Nam). Mỗi chiến lược có một **tài khoản ảo** bằng USDT. Ở chế độ tín hiệu, bot gửi thêm **lệnh đề xuất cho tài khoản thật** của bạn.
- **Trang web**: NAV các tài khoản ảo so với BTC, vị thế, lệnh, giao dịch; trang phân tích coin.
- **Bot Telegram**: `/nhanh`, `/phantich`, `/danhmuc`, `/lenh`, `/chay`, `/khop`, `/von`, `/trangthai`.
- **Phân tích đa agent** (`/phantich`): các agent AI đọc giá, tin tức, tâm lý thị trường, tokenomics rồi tranh luận; **hành động cuối cùng luôn do chiến lược quyết định**, không do AI.

> **Bot KHÔNG BAO GIỜ đặt lệnh lên sàn và không bao giờ hỏi API key sàn.** Ở chế độ tín hiệu, bạn tự đặt lệnh trên Binance rồi báo lại bằng `/khop`.
>
> **Không phải khuyến nghị đầu tư.** Crypto biến động rất mạnh; chỉ dùng số tiền bạn chấp nhận mất. Mọi con số dưới đây là quá khứ, sau chi phí giả định, và không phải lời hứa lợi nhuận.

## Bot quyết định thế nào

Chiến lược chính là **`btc_sma_ens` - BTC theo 7 đường SMA** (`cryptotrader/strategies/sma_ensemble.py`). Nó là ứng viên `C2_btc_sma_ens` mà người kiểm định độc lập đã chọn (xem [kết quả](#kết-quả-đã-kiểm-định)).

1. Mỗi ngày, sau khi nến ngày UTC của BTCUSDT đóng, bot tính 7 đường trung bình động giản đơn của giá đóng cửa: **50, 75, 100, 125, 150, 175, 200 ngày** (tính cả ngày vừa đóng).
2. **Tỷ trọng BTC lý tưởng = số đường SMA mà giá đóng cửa đang nằm trên, chia 7.** Giá trên cả 7 đường → 100% BTC; trên 4 đường → 57%; dưới cả 7 → 100% USDT. Phần còn lại giữ USDT. Không đòn bẩy, không bán khống, chỉ BTC.
3. **Biên 10% để tiết kiệm phí:** chỉ giao dịch khi tỷ trọng đang giữ lệch tỷ trọng lý tưởng **hơn 10 điểm %**, hoặc khi chuyển giữa "không giữ" và "có giữ". Lệch nhỏ hơn thì giữ nguyên.
4. Chưa đủ 201 nến ngày thì giữ USDT. Ngày BTC không có nến thì không đổi gì.
5. Lệnh là lệnh thị trường cho nến kế tiếp. Tài khoản ảo khớp ở **giá mở cửa 00:00 UTC** cộng trượt giá 0,1% và phí 0,1%.

Báo cáo hằng ngày ghi rõ tín hiệu, ví dụ: *"giá trên 4/7 đường SMA → BTC lý tưởng 57% NAV; đang 62%, lệch không quá 10% nên giữ nguyên"*.

**Ba tài khoản ảo** chạy song song để so sánh trên dữ liệu mới:

| tài khoản | tên | vai trò |
|---|---|---|
| BTC theo 7 đường SMA | `btc_sma_ens` | chiến lược chính; mặc định cho tín hiệu thật (`SIGNAL_STRATEGY`) và cho `/phantich` |
| Xu hướng BTC (SMA200) | `btc_trend` | mốc so sánh: chiến lược giữ chỗ cũ (giữ BTC khi giá trên SMA200) |
| Mua & giữ BTC | `btc_buy_hold` | mốc so sánh |

**Tín hiệu cho tài khoản thật** (`TRADE_MODE=signal`): bot áp **đúng quy tắc biên 10%** lên tỷ trọng thật của bạn (theo `/von` và `/khop`). Khối lượng làm tròn theo `stepSize` của cặp; lệnh dưới giá trị tối thiểu của sàn được ghi "Bỏ qua" kèm lý do. Coin khác BTC đang giữ trong tài khoản thật sẽ được đề xuất bán, vì chiến lược chỉ giữ BTC và USDT.

**`/nhanh BTC`** cho biết giá đang trên mấy/7 đường SMA và tỷ trọng mục tiêu. **`/phantich`** dùng chiến lược này làm "quyết định hệ thống": BTC trong rổ mà chưa giữ → MUA; đang giữ → GIỮ; ra khỏi rổ mà đang giữ → BÁN; coin khác → ĐỨNG NGOÀI. Bot truyền danh mục (tài khoản thật ở chế độ tín hiệu, nếu không thì tài khoản ảo chính) cho phân tích của **đúng ngày bot đã xử lý**; phân tích một ngày trong quá khứ không được thấy danh mục hiện tại.

## Kết quả đã kiểm định

Một người kiểm định độc lập (không thiết kế ứng viên nào) đã viết và băm quy tắc chọn **trước**, rồi chạy giai đoạn TEST **đúng một lần** cho 12 ứng viên. Không ứng viên nào qua quy tắc chặt (Tier 1) hay bản nới (Tier 2); quy tắc dự phòng đã đăng ký trước ("quy tắc đơn giản nhất qua các cổng kiểm toán, sụt giảm, Sharpe và sức chịu") chọn `C2_btc_sma_ens`. Hồ sơ đầy đủ: [`research/RESULTS.md`](research/RESULTS.md); bản gốc quy tắc, quyết định và code: [`research/evaluated/`](research/evaluated/).

Điều kiện: dữ liệu kho lưu trữ Binance, 763 cặp USDT **kể cả cặp đã huỷ niêm yết**; vốn 10.000 USDT; phí 0,1% + trượt giá 0,1% mỗi lần khớp; khớp ở giá mở cửa ngày hôm sau; USDT nhàn rỗi lãi 0%.

| giai đoạn | btc_sma_ens: CAGR / sụt giảm tối đa / Sharpe | BTC mua và giữ | `btc_trend` (SMA200) |
|---|---|---|---|
| TRAIN 2018–2021 (**trong mẫu**) | +58,4% / −41,4% / 1,19 | +36,3% / −81,2% / 0,80 | +21,1% / −64,7% / 0,63 |
| VALID 2022–2023 (**một phần trong mẫu**) | +25,3% / −21,5% / 0,93 | −5,9% / −66,9% / 0,17 | +39,0% / −18,9% / 1,27 |
| **TEST 01/01/2024–14/10/2026 (ngoài mẫu, chạy một lần)** | **+25,4% / −29,6% / 0,86** | +24,7% / −53,0% / 0,70 | +19,7% / −32,3% / 0,68 |

Lợi nhuận theo năm (chạy liên tục): 2022: −21,1% (BTC −64,2%); 2023: +98,8% (BTC +155,6%); 2024: +76,3% (BTC +121,3%); 2025: −1,0% (BTC −6,3%); 2026 tới 14/10: +12,6% (BTC −6,8%).

**Đọc bảng này thế nào** (nguyên văn tóm tắt của người kiểm định):

- lợi nhuận gần bằng BTC, nhưng sụt giảm chỉ khoảng một nửa; C2 đứng ngoài phần lớn đợt BTC giảm 53 % (10/2025 → 06/2026);
- phần vượt BTC (+0,7 điểm/năm) **không có ý nghĩa thống kê**: khoảng bootstrap 90 % là −38…+30 điểm;
- thua BTC trong năm tăng mạnh: 2024 được +68 đến +76 %, so với +112 đến +121 % của BTC;
- thua `btc_trend` trên VALID (2022–2023), vì SMA200 đứng ngoài trọn năm 2022;
- thắng `btc_trend` trên TRAIN, TEST và toàn giai đoạn 2018–2026: CAGR 40,1 % so với 25,1 %, sụt giảm −46,9 % so với −64,7 %.
- **Khuyến nghị:** chạy ở chế độ giấy/tín hiệu trước, rồi đánh giá lại sau 6–12 tháng dữ liệu mới; lợi thế chính là tránh sập, không phải thắng BTC mỗi năm.

Kỳ vọng hợp lý: **lợi nhuận xấp xỉ BTC qua một chu kỳ đầy đủ, với khoảng một nửa mức sụt giảm của BTC**; thua BTC hàng chục điểm % trong năm tăng mạnh; mất ít hơn nhiều trong năm giảm. Không có con số lợi nhuận kỳ vọng nào được hứa.

**Bản bot giống hệt bản đã kiểm định:** trên dữ liệu thật TRAIN, VALID và TRAIN+VALID, module của bot và code người kiểm định đã chạy cho **cùng từng lệnh khớp** (309 lệnh) và cùng NAV từng ngày (`python -m research.parity`); một test trong bộ test mặc định kiểm tra điều này trên dữ liệu tổng hợp.

### Giới hạn trung thực (nguyên văn báo cáo người kiểm định, mục 11)

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

Tóm tắt tiếng Việt: chỉ có **một đường đi thị trường** (2,8 năm: một đợt tăng, một đỉnh, một đợt giảm), nên không kết luận nào mạnh về thống kê; số TRAIN/VALID lạc quan vì nhóm nghiên cứu đã thử 805 cấu hình và dùng VALID để chọn; C2 **không** thắng hẳn chiến lược SMA200 cũ trên VALID; chi phí giả định thận trọng cho BTC ở vốn nhỏ, nhưng không tính thuế, sự cố sàn hay vận hành.

## Rủi ro - đọc trước khi dùng tiền thật

- **Biến động:** BTC từng giảm 81% (2018), 67% (2022) và 53% (10/2025 → 06/2026). Chiến lược giảm sụt giảm trong quá khứ (−29,6% trên TEST) nhưng **vẫn có thể mất hàng chục phần trăm**, và một cú sập trong một ngày (giá mở cửa hôm sau thấp hơn nhiều) không được biên nào bảo vệ.
- **Thiên lệch sống sót và chọn lọc:** chiến lược chỉ giao dịch BTC, nên thiên lệch sống sót của dữ liệu rất nhỏ; nhưng việc chọn "chỉ BTC" và chọn chiến lược sau khi thử nhiều cấu hình là thiên lệch chọn lọc. TEST là ước lượng tốt nhất hiện có, không phải bảo đảm.
- **Sàn và lưu ký:** tiền nằm trên sàn chịu rủi ro sàn (phá sản, bị hack, đóng băng rút tiền, thay đổi pháp lý). Bot không quản lý ví hay khoá riêng nào.
- **Không tự động giao dịch:** bot không đặt lệnh. Bạn đặt lệnh sau báo cáo 07:10 giờ VN, nên giá thật khác giá mở cửa 00:00 UTC mà backtest dùng; quên đặt hoặc đặt sai cũng là rủi ro của bạn. Báo khớp bằng `/khop` để sổ tài khoản thật đúng.
- **Chi phí:** giả định 0,1% phí + 0,1% trượt giá mỗi lần khớp; chiến lược khớp khoảng 60 lệnh/năm khi thị trường đi ngang (vòng quay ~6 lần/năm, kéo CAGR ~2,5 điểm/năm). Phí thật có thể thấp hơn (BNB, VIP) hoặc cao hơn.
- **Thuế** và quy định pháp lý về tài sản số tại nơi bạn sống không được tính.
- **Không phải khuyến nghị đầu tư.** Phân tích AI (`/phantich`) có thể sai; hành động hệ thống chỉ là quy tắc trên, và văn bản của AI bị kiểm tra (Output Guard) nhưng vẫn có thể chứa số sai.

## Bắt đầu nhanh

### Trên máy của bạn

```bash
git clone <repo> trading_crypto && cd trading_crypto
python -m venv .venv && . .venv/bin/activate      # Python 3.11+
pip install -e ".[bot]"                           # thêm ",dev" để chạy test
cp .env.example .env                              # điền WEB_PASSWORD, TELEGRAM_TOKEN...
python main_botkeep.py                            # web ở http://localhost:8080
```

Log khởi động cần có dòng `Trading bot: mode paper, books btc_sma_ens, btc_trend, btc_buy_hold, signals follow btc_sma_ens, runs daily at 07:10`. Mở trang web (tên đăng nhập bất kỳ, mật khẩu `WEB_PASSWORD`), gửi `/id` cho bot Telegram để lấy id rồi đặt `TELEGRAM_ALLOWED_IDS`, sau đó `/nhanh BTC` và `/chay`.

Lần chạy đầu, mỗi tài khoản ảo bắt đầu ở nến ngày đã đóng gần nhất (không chạy lùi về quá khứ) và tải lịch sử nến BTC (cache trong `CRYPTO_DATA_DIR`, mặc định `./data`).

### Trên VPS (Linux, systemd)

```bash
sudo useradd -r -m -d /opt/trading_crypto tradingbot
sudo -u tradingbot git clone <repo> /opt/trading_crypto/app
cd /opt/trading_crypto/app
sudo -u tradingbot python3 -m venv .venv
sudo -u tradingbot .venv/bin/pip install -e ".[bot]"
sudo -u tradingbot cp .env.example .env && sudo -u tradingbot nano .env
```

`/etc/systemd/system/trading-crypto.service`:

```ini
[Unit]
Description=trading-crypto (web + Telegram + bot giao dịch ảo/tín hiệu)
After=network-online.target
Wants=network-online.target

[Service]
User=tradingbot
WorkingDirectory=/opt/trading_crypto/app
ExecStart=/opt/trading_crypto/app/.venv/bin/python main_botkeep.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now trading-crypto
journalctl -u trading-crypto -f
```

Đặt trang web sau một reverse proxy có HTTPS (Caddy, nginx) và đặt `PUBLIC_URL`. Giờ của máy chủ không quan trọng: bot dùng giờ Việt Nam cho lịch và ngày UTC cho nến. Khi máy chủ khởi động lại, bot chạy bù tối đa 30 ngày bị lỡ.

### Trên Botkeep

```bash
python scripts/build_botkeep_bundle.py     # -> dist/trading-crypto-botkeep.zip (giới hạn 250 file, 4 MB)
```

Upload, giải nén vào thư mục gốc, file khởi động `main_botkeep.py`, tạo `.env`. Hướng dẫn từng bước, toàn bộ lệnh Telegram, biến môi trường và xử lý sự cố: [`deploy/botkeep/README.md`](deploy/botkeep/README.md).

### Nên dùng thế nào

1. Để `TRADE_MODE=paper` **ít nhất vài tháng**; theo dõi ba tài khoản ảo trên trang web. Đây là dữ liệu mới thật sự, sau TEST.
2. Khi muốn làm theo bằng tiền thật: `TRADE_MODE=signal` (mặc định theo `btc_sma_ens`), khởi động lại, gửi `/von <số USDT>`. Mỗi sáng sau 07:10 giờ VN bot gửi lệnh đề xuất; bạn tự đặt trên Binance rồi báo `/khop mua BTC 0.0123 65000` (khối lượng khớp của lệnh; phí lệnh mua mặc định trừ vào coin như Binance làm khi không dùng BNB, trả phí bằng BNB thì thêm `phi=bnb`).
3. Đánh giá lại sau 6–12 tháng.

## Quy tắc thị trường (`cryptotrader/market/rules.py`)

- Giao dịch 24/7. Nến ngày là một ngày UTC: đóng lúc **00:00 UTC (07:00 giờ Việt Nam)**, coi là chốt từ 00:05 UTC. Nến đang hình thành không bao giờ được dùng.
- Chi phí mặc định mỗi lệnh khớp: phí taker **0,1%** + trượt giá **0,1%** ngược chiều người giao dịch (khoảng 0,4% cho một vòng mua-bán). Đổi bằng `CRYPTO_TAKER_FEE_PCT`, `CRYPTO_SLIPPAGE_PCT`.
- Không có thời gian chờ thanh toán. Khối lượng lẻ, làm tròn xuống theo `stepSize`, phải đạt `minQty` và `minNotional`; giá theo `tickSize` (exchangeInfo).
- Vũ trụ giao dịch: cặp USDT, loại stablecoin/tiền pháp định, token đòn bẩy (UP/DOWN/BULL/BEAR), token bọc/neo giá (WBTC, BETH, PAXG...).

## Dữ liệu (`cryptotrader/data/`)

| Nguồn | Dùng cho |
|---|---|
| `data-api.binance.vision` | Nến ngày (phân trang), exchangeInfo, ticker 24h. Không cần key. |
| `data.binance.vision` (kho lưu trữ) | Danh sách mọi cặp từng có, **kể cả đã huỷ niêm yết**; lịch sử khi REST không có cặp đó. Kiểm tra checksum. |
| OKX | Dự phòng khi Binance không trả lời; chỉ bù phần nến còn thiếu, không ghi vào cache. Funding/open interest cho `/phantich`. |
| alternative.me, CoinGecko, Google News RSS | Chỉ số Sợ hãi & Tham lam, tokenomics, tin tức cho `/phantich`. |

`load_ohlcv("BTC", "2026-10-14")` trả về nến đã đóng `[Date, Open, High, Low, Close, Volume, QuoteVolume, Trades]`, cache tại `CRYPTO_DATA_DIR`, ghi file nguyên tử. Mỗi nguồn có cầu dao (circuit breaker) và giới hạn thời gian. `api.binance.com` bị chặn theo vùng (451) nên không được dùng.

**Rổ thanh khoản point-in-time cho chiến lược nhiều cặp** (`cryptotrader/data/universe.py`, `liquid_universe(day, top)`): N cặp USDT đang `TRADING` trong exchangeInfo, qua quy tắc loại trừ, có ít nhất 180 ngày giao dịch, xếp theo khối lượng USDT trung vị 30 ngày tính tới ngày quyết định (cùng định nghĩa `liq_topN` của bộ backtest). Rổ của mỗi ngày được cache riêng cho ngày đó. Nếu exchangeInfo không tải được hoặc cũ hơn 3 ngày, nếu nguồn giá không trả lời cho bất kỳ cặp nào, hoặc không đủ N cặp, hàm **báo lỗi** để lần chạy được thử lại; không bao giờ dùng rổ cũ hay rổ thiếu. Chiến lược hiện tại chỉ giao dịch BTC nên không dùng rổ này. Giới hạn: exchangeInfo chỉ cho biết cặp nào giao dịch **hôm nay**, nên khi chạy bù một ngày cũ (tối đa 30 ngày), cặp đã bị huỷ niêm yết sau ngày đó sẽ thiếu.

## Cấu trúc

```
cryptotrader/            lõi (được đóng gói lên Botkeep)
  market/rules.py        phí, trượt giá, bộ lọc cặp, làm tròn, nến ngày UTC, loại trừ
  data/                  Binance REST, kho lưu trữ, OKX, cache, cầu dao, rổ thanh khoản
  trading/ledger.py      sổ giao dịch spot (tài khoản ảo, backtest, tài khoản thật)
  strategies/            btc_sma_ens (chính), btc_trend, btc_buy_hold
  agents/, graph/        phân tích đa agent; quant_engine = quyết định hệ thống
  dataflows/, llm_clients/  nguồn dữ liệu cho agent; chuỗi AI dự phòng
bot/                     web, Telegram, lịch, tài khoản ảo, tín hiệu
research/                bộ backtest, giai đoạn TEST khoá, hồ sơ kiểm định (RESULTS.md, evaluated/)
deploy/botkeep/          triển khai Botkeep
scripts/                 đóng gói Botkeep
tests/                   pytest; mặc định không truy cập mạng
```

## Nghiên cứu

```bash
pip install -e ".[dev]"
python -m research download                         # dữ liệu kho lưu trữ (~30 phút lần đầu)
python -m research run btc_sma_ens --split VALID    # chiến lược của bot, chạy y như bot
python -m research.parity                           # đối chiếu từng lệnh với code đã kiểm định
```

Giai đoạn TEST (từ 01/01/2024) bị khoá trong bộ backtest và **đã được người kiểm định dùng một lần** ngày 15/10/2026. Chạy lại TEST để chỉnh chiến lược là nhìn trộm: chiến lược mới phải chờ dữ liệu mới. Chi tiết: [`research/README.md`](research/README.md).

## Kiểm thử

```bash
pip install -e ".[dev,bot]"
pytest -q                     # bộ test mặc định, không dùng mạng
pytest -q -m integration      # gọi API thật (Binance, kho lưu trữ, OKX, tin tức...)
ruff check .
```

## Giấy phép

Apache 2.0 (xem [LICENSE](LICENSE) và [NOTICE](NOTICE)). Phần agent dựa trên [TradingAgents](https://github.com/TauricResearch/TradingAgents) của Tauric Research. Lịch sử thay đổi: [CHANGELOG.md](CHANGELOG.md).
