# Nhật ký thay đổi

Định dạng theo [Keep a Changelog](https://keepachangelog.com/vi/1.1.0/). Ngày theo UTC.

## [Chưa phát hành] - 2026-10-15

### Thêm

- **Chiến lược chính `btc_sma_ens`** (`cryptotrader/strategies/sma_ensemble.py`): BTC theo 7 đường SMA (50..200 ngày), tỷ trọng = số đường giá nằm trên chia 7, biên 10%. Đây là ứng viên `C2_btc_sma_ens` do người kiểm định độc lập chọn theo quy tắc dự phòng đã đăng ký trước (TEST 2024 – 14/10/2026 sau phí: CAGR 25,4%, MDD −29,6%, Sharpe 0,86; BTC 24,7% / −53,0% / 0,70; phần vượt BTC không có ý nghĩa thống kê). Đăng ký trong `cryptotrader.strategies.REGISTRY`, là tài khoản ảo đầu tiên, mặc định của `SIGNAL_STRATEGY` và của quyết định hệ thống trong `/phantich`.
- **Hồ sơ kiểm định** `research/RESULTS.md` và `research/evaluated/` (quy tắc chọn đã băm, dòng thời gian, `winner.json`, bản sao giống từng byte của code đã kiểm định; mã băm được test kiểm tra).
- **Đối chiếu bot ↔ code đã kiểm định:** tên chiến lược `btc_sma_ens` (module live, chạy y như bot) và `btc_sma_ens_eval` (code đóng băng) trong `python -m research run`; `python -m research.parity`. Trên dữ liệu thật TRAIN/VALID/TRAIN+VALID: 309 lệnh khớp giống hệt, NAV giống hệt; test trên panel tổng hợp trong bộ test mặc định.
- **Rổ thanh khoản point-in-time cho bot** (`cryptotrader/data/universe.py`): top N cặp `TRADING` theo khối lượng trung vị 30 ngày, cache theo ngày, báo lỗi (để chạy lại) thay vì dùng rổ cũ hoặc thiếu.
- **Dòng giải thích tín hiệu** trong báo cáo hằng ngày (hook `explain(state)` của chiến lược) và trong `/nhanh` (`explain_closes`).
- **Tín hiệu tài khoản thật theo đúng quy tắc của chiến lược** (hook `real_target`): biên 10% áp lên tỷ trọng thật, không đề xuất lệnh nhỏ tốn phí.
- **`/phantich` biết danh mục:** bot truyền danh mục (tài khoản thật ở chế độ tín hiệu, nếu không thì tài khoản ảo chính) và chiến lược tín hiệu cho graph, chỉ khi ngày phân tích là ngày bot đã xử lý; nhờ đó phân biệt được MUA và GIỮ.
- Engine của agent: `engine_view` có thể để engine tự suy ra hành động từ danh mục (`in_basket`) và thêm mốc giá (`levels_extra`, ở đây là 7 đường SMA).
- Test: `tests/test_strategy_sma_ensemble.py`, `tests/test_universe_live.py`, `tests/test_bot_integration.py` (ba tài khoản trên báo cáo, web và Telegram; biên tài khoản thật; `/nhanh`; `/phantich` qua graph thật với mô hình giả lập, không mạng).
- README đầy đủ bằng tiếng Việt (cách bot quyết định, kết quả đã kiểm định, giới hạn, rủi ro, chạy trên máy/VPS/Botkeep).

### Thay đổi

- `btc_trend` (SMA200) từ chiến lược tạm trở thành **tài khoản so sánh** (`benchmark=True`), cùng `btc_buy_hold`.
- Mặc định `SIGNAL_STRATEGY` và `engine_strategy` (`CRYPTO_ENGINE_STRATEGY`): `btc_trend` → `btc_sma_ens`. Người đang chạy `TRADE_MODE=signal` với tài khoản thật giữ coin sẽ thấy thông báo đổi chiến lược một lần; muốn giữ chiến lược cũ thì đặt `SIGNAL_STRATEGY=btc_trend`.
- Bot lập lệnh với `min_trade_pct` của chiến lược (mặc định 1% như trước), giống bộ backtest.
- Tài liệu Botkeep, `.env.example`, `research/README.md` cập nhật theo chiến lược mới.

### Sửa lỗi

- **Nến về trễ:** cache nến không còn đánh dấu "đã đủ" tới ngày mà nguồn chưa trả về, nên các lần thử lại mỗi 10 phút của lịch chạy thực sự tải lại nến trễ (trước đây tín hiệu có thể trễ 24 giờ).
- **`.gitignore`:** `data/` chỉ còn áp dụng ở thư mục gốc; gói mã nguồn `cryptotrader/data/` không còn bị bỏ qua khi commit.
- **`/khop`:** khối lượng là khối lượng khớp của lệnh; phí lệnh mua mặc định trừ vào coin (`phi=coin`), có thể chọn `phi=usdt` hoặc `phi=bnb`. Lệnh bán hết được làm tròn xuống theo `stepSize`; phần bụi dưới mức tối thiểu không còn bị đề xuất bán mỗi ngày.
- Tài khoản thật: coin chỉ có trong tài khoản thật cũng được định giá theo giá đóng cửa (NAV, `/danhmuc`, tỷ trọng gửi AI).
- Số lượng và giá trong báo cáo in không có dấu phân cách hàng nghìn, chép thẳng vào `/khop` được.
- Trang web: người lạ không thể khoá đăng nhập của chủ bot (cookie thiết bị đã đăng nhập); thêm `TRUSTED_PROXY_IPS` cho uvicorn sau reverse proxy.
- `/chay` gửi báo cáo cho cả các chat nhận báo cáo (`TELEGRAM_REPORT_CHAT_IDS`); Telegram thử lại khi lỗi mạng/5xx và giữ tin chưa gửi được trong hộp thư chờ để gửi lại.
- Lịch chạy: số lần thử nến trễ được đặt lại khi một ngày kết thúc vì lỗi; cờ "đang chạy" không bị một lượt `/chay` xoá khi lượt chạy theo lịch còn đang chạy.
- `CRYPTO_TAKER_FEE_PCT`, `CRYPTO_SLIPPAGE_PCT`, `CRYPTO_BINANCE_DATA_URL`, `CRYPTO_OKX_URL` trong `.env` nay có hiệu lực (trước đây bị đọc trước khi nạp `.env`). Sổ đã có giữ mức phí đã lưu trong sổ.
- `/phantich`: hành động hệ thống theo đúng biên 10% như tín hiệu hằng ngày; Output Guard không còn bỏ phần diễn giải vì câu tóm tắt tranh luận, điều kiện đặt sau, phủ định xa hoặc câu "không dùng đòn bẩy"; năm và chu kỳ chỉ báo không còn bị coi là mốc giá; dòng do mô hình viết giống dòng hệ thống ("**Rating**: SELL") nay bị kiểm tra; mốc giá chỉ được chấp nhận khi có trong dữ liệu (không lấy từ lời văn của agent hay tiêu đề tin).
- Chuỗi nhà cung cấp AI chuyển sang nhà cung cấp kế tiếp khi gateway trả HTTP 200 kèm lỗi hoặc thiếu `choices`; khi mọi nhà cung cấp đều lỗi, lượt phân tích vẫn hoàn tất với quyết định của hệ thống (ghi chú "AI không khả dụng") thay vì thất bại.
- Kiểm tra `/phantich` có chạy được theo đúng nhà cung cấp đã cấu hình (`CRYPTO_AGENTS_LLM_PROVIDER`), nêu tên biến còn thiếu; nhà cung cấp ngoài chuỗi phải có tên model thật.
- Log trạng thái đầy đủ của mỗi lượt phân tích chỉ giữ 30 file mới nhất mỗi cặp (`state_logs_kept`).
- `research download`: `data_end` không tiến lên khi một cặp còn giao dịch bị lỗi tải (tránh coi nhầm là huỷ niêm yết).
- CoinGecko: lượt phân tích quá 360 ngày không gọi API lịch sử (gói miễn phí từ chối, trước đây làm mở cầu dao của nguồn); lỗi 4xx không còn bị coi là nguồn sập.
- Gắn tin: tiêu đề về Bitcoin Cash, Wrapped Bitcoin, Ethereum Classic... không còn bị tính là tin của BTC/ETH.
- `research/RESULTS.md`, README: sửa câu "C2 is in cash about 42 % of the time" (42% là tỷ trọng USDT trung bình; hoàn toàn giữ tiền mặt khoảng 25% số ngày TEST).

### Ghi chú nâng cấp

- Tài khoản ảo mới `paper_btc_sma_ens.json` bắt đầu ở nến đã đóng gần nhất sau lần chạy đầu; các tài khoản cũ giữ nguyên lịch sử.
- Không thêm thư viện phụ thuộc mới.
