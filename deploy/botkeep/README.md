# trading-crypto trên Botkeep

Hướng dẫn chạy bot crypto liên tục trên Botkeep (hoặc bất kỳ máy chủ Python nào luôn bật). Một tiến trình `main_botkeep.py` gồm:

- **Bot giao dịch** chạy mỗi ngày sau khi nến ngày Binance đóng (00:00 UTC = 07:00 giờ Việt Nam): mỗi chiến lược một **tài khoản ảo** bằng USDT, và (tuỳ chọn) **lệnh đề xuất cho tài khoản thật** của bạn.
- **Trang web**: tổng quan NAV các tài khoản ảo so với BTC, vị thế, lệnh, giao dịch, nút chạy ngay; trang phân tích coin.
- **Bot Telegram**: `/nhanh`, `/phantich`, `/danhmuc`, `/lenh`, `/chay`, `/khop`, `/von`, `/trangthai`, `/id`, `/help`.

> **Bot KHÔNG BAO GIỜ đặt lệnh lên sàn và không bao giờ hỏi API key sàn.** Ở chế độ tín hiệu, bot chỉ gửi lệnh đề xuất; bạn tự đặt trên Binance rồi báo lại bằng `/khop`.

Chỉ giao dịch spot Binance, cặp USDT. Stablecoin, token đòn bẩy và token bọc/neo giá bị loại.

---

## 1. Chuẩn bị

| Cần có | Lấy ở đâu | Bắt buộc? |
|---|---|---|
| Mật khẩu trang web | Tự đặt | Có, nếu muốn dùng trang web |
| Bot Telegram | Chat với [@BotFather](https://t.me/BotFather) → `/newbot` → lấy token | Nên có |
| Nhà cung cấp AI | 9Router (endpoint kiểu OpenAI), hoặc key Groq / OpenRouter / xAI | Chỉ cho `/phantich` |

Dữ liệu giá lấy từ `data-api.binance.vision` (bản sao dữ liệu công khai của Binance, không cần key), dự phòng OKX. `api.binance.com` bị chặn theo vùng (lỗi 451) nên bot không dùng.

## 2. Tạo gói upload

Trên máy có mã nguồn (Python 3.11+):

```bash
cd trading_crypto
python scripts/build_botkeep_bundle.py
```

Lệnh tạo `dist/botkeep/` và `dist/trading-crypto-botkeep.zip`, rồi báo lỗi nếu vượt giới hạn upload của Botkeep (**250 file, 4 MB**). Gói chỉ gồm `cryptotrader/`, `bot/`, `main_botkeep.py`, `requirements.txt`, `.env.example`, README này, LICENSE và NOTICE; test, research và dữ liệu được bỏ ra.

## 3. Tạo server trên Botkeep

1. Tạo workload **Python 3.11 trở lên**.
2. Upload file zip rồi giải nén vào **thư mục gốc** của server (hoặc upload nội dung `dist/botkeep/`).
3. File khởi động: `main_botkeep.py`. File requirements: `requirements.txt`.
4. Copy `.env.example` thành `.env` (cùng thư mục với `main_botkeep.py`) và điền tối thiểu:

```env
# Trang web
WEB_PASSWORD=mat-khau-dai-kho-doan
PUBLIC_URL=https://<ten-server>.botkeep...   # địa chỉ Botkeep cấp

# Telegram
TELEGRAM_TOKEN=123456:ABC...
TELEGRAM_ALLOWED_IDS=        # để trống lúc đầu, xem bước 4

# Bot giao dịch
TRADE_MODE=paper             # paper | signal | off
SIGNAL_STRATEGY=btc_sma_ens  # chiến lược cho tín hiệu thật (mặc định)

# AI cho /phantich (ít nhất một dòng có key; bỏ trống thì /phantich báo chưa dùng được)
ROUTER9_BASE_URL=https://<9router-cua-ban>/v1
ROUTER9_API_KEY=...
GROQ_API_KEY=...             # dự phòng
OPENROUTER_API_KEY=...       # dự phòng
```

5. Bấm **Start**. Log khởi động cần có:
   - `Trading bot: mode paper, books btc_sma_ens, btc_trend, btc_buy_hold, ... runs daily at 07:10 (Vietnam time)`.
   - `AI provider chain: ...` nếu đã có key AI. Nếu thấy `AI analysis (/phantich) unavailable` thì `/phantich` chưa dùng được (lý do ghi ngay sau); `/nhanh` và bot giao dịch vẫn chạy.
   - Nếu thấy `WEB_PASSWORD is not set` thì trang web đang khoá: thêm mật khẩu rồi khởi động lại.

Botkeep tự đặt `SERVER_PORT`; chạy nơi khác thì dùng `PORT` (mặc định 8080).

## 4. Lấy Telegram id và mở quyền

1. Mở bot trên Telegram, gửi `/id`. Bot trả về id của bạn (với tài khoản chưa được phép, bot chỉ trả lời lệnh này, tối đa một lần mỗi 5 phút).
2. Điền id vào `.env`: `TELEGRAM_ALLOWED_IDS=123456789` (nhiều người thì cách nhau dấu phẩy).
3. Muốn báo cáo hằng ngày gửi vào một nhóm: thêm bot vào nhóm, lấy id nhóm (số âm) và đặt `TELEGRAM_REPORT_CHAT_IDS`. Bỏ trống thì gửi cho các id trong `TELEGRAM_ALLOWED_IDS`.
4. Khởi động lại server.

## 5. Kiểm tra sau khi chạy

1. Mở `PUBLIC_URL`. Trình duyệt hỏi đăng nhập: **tên gì cũng được**, mật khẩu là `WEB_PASSWORD`.
2. Gửi `/nhanh BTC` cho bot (hoặc trang **Phân tích** → **Xem nhanh**). Ra giá, xu hướng trong vài giây là máy chủ đã đọc được dữ liệu Binance.
3. Gửi `/chay`: bot xử lý nến ngày đã đóng gần nhất và gửi báo cáo. Lần đầu, mỗi tài khoản ảo bắt đầu từ nến này (không chạy lùi về quá khứ), đặt lệnh, và lệnh khớp ở giá mở cửa nến kế tiếp.
4. `/trangthai`: hàng đợi phân tích, nhà cung cấp AI và trạng thái bot.

## 6. Cách dùng

### Lệnh Telegram

| Lệnh | Việc |
|---|---|
| `/nhanh BTC` | Xem nhanh (không dùng AI, vài giây): giá đóng cửa, thay đổi 1/7/30 ngày, xu hướng (giá so với SMA50/SMA200), biến động, chiến lược chính đọc được gì (với BTC: giá trên mấy/7 đường SMA và tỷ trọng mục tiêu), coin có nằm trong rổ của các tài khoản ảo không |
| `/phantich BTC` | Phân tích bằng các agent AI (vài phút); số liệu nhanh tới trước, báo cáo (kèm file HTML và link web) tới sau |
| `/phantich BTC 2026-10-01` | Phân tích tại một ngày trong quá khứ (ngày UTC; chỉ dùng dữ liệu tới ngày đó) |
| `/danhmuc` | Các tài khoản ảo (và tài khoản thật ở chế độ `signal`) |
| `/lenh` | Lệnh bot đặt cho nến tới |
| `/chay` | Chạy ngay các nến đã đóng mà bot chưa xử lý (ngày đã chạy thì bỏ qua) |
| `/von 1000` | Đặt số USDT của tài khoản thật (chế độ `signal`) |
| `/khop mua BTC 0.0123 65000 [phi=coin\|usdt\|bnb] [YYYY-MM-DD]` | Báo đã khớp lệnh thật (chế độ `signal`); bán: `/khop ban BTC 0.0123 66000` |
| `/futures` | Sổ futures long/short (ảo): NAV, vị thế, cắt lỗ, giá thanh lý, tiến độ mục tiêu; tài khoản futures thật ở chế độ `signal` |
| `/futures chay` | Chạy ngay phần futures cho nến đã đóng |
| `/futures tieptuc BNB` | Chạy lại sổ futures đã dừng (đạt mục tiêu hoặc chạm mức dừng); mục tiêu/mức dừng tính lại từ vốn hiện tại |
| `/fvon 10` | Số dư ví futures thật (chế độ `FUTURES_MODE=signal`); `/fvon 10 moi` đặt lại vốn gốc |
| `/trangthai` | Hàng đợi, nhà cung cấp AI, số lượt gọi AI đã dùng, trạng thái bot |
| `/id`, `/help` | Telegram id của bạn; hướng dẫn |

Coin nhận các dạng `BTC`, `btc`, `BTCUSDT`, `BTC/USDT`, `btc-usdt`. Cặp không phải USDT (ví dụ `ETH/BTC`) và stablecoin bị từ chối.

**Về `/khop`:**

- Số dùng **dấu chấm** cho phần thập phân và không có dấu phân cách hàng nghìn: `0.0123`, `65000`, `3200.5`. Bot từ chối `65,000` hoặc `0,5` thay vì đoán (đọc sai một dấu là sai 1000 lần).
- Khối lượng là **khối lượng khớp của lệnh** (như trong lịch sử lệnh trên Binance, cũng là số bot đề xuất), giá tính bằng USDT. Số lượng và giá trong báo cáo của bot in không có dấu phân cách hàng nghìn, nên chép thẳng vào `/khop` được.
- Phí: lệnh **mua** mặc định trừ phí vào coin (Binance làm vậy khi bạn không dùng BNB trả phí): mua 0.00123 BTC thì sổ ghi thực nhận 0.00122877 BTC và trừ đủ 0.00123 × giá USDT. Lệnh **bán** mặc định trừ phí bằng USDT. Nếu trả phí bằng BNB, thêm `phi=bnb` (phí không ghi vào sổ vì BNB nằm ngoài sổ). `phi=usdt` cho lệnh mua trừ phí bằng USDT.
- Khi bán hết, bot đề xuất khối lượng đã làm tròn xuống theo `stepSize`; phần lẻ còn lại dưới mức tối thiểu của sàn (bụi) không bán được, bot bỏ qua và không nhắc lại mỗi ngày.
- Ngày là **ngày UTC** (mặc định hôm nay theo UTC). Lệnh khớp lúc 05:00 sáng giờ VN thuộc ngày UTC hôm trước.
- Bot từ chối lệnh không thể xảy ra và **không ghi gì** khi từ chối: không đủ USDT, bán nhiều hơn số đang giữ, ngày ở tương lai hoặc trước lệnh đã ghi gần nhất, giá lệch hơn 5 lần so với giá đóng cửa gần nhất (gõ nhầm 6500 thay vì 65000).
- Sửa (edit) một tin nhắn `/khop` cũ **không** ghi lệnh lần nữa.

### Trang web

- **Tổng quan** (`/`): biểu đồ NAV các tài khoản ảo so với BTC (chỉ số hoá về 100 tại ngày đầu tiên chung), từng tài khoản (NAV, lợi nhuận, sụt giảm tối đa, phí đã trả, mục tiêu tỷ trọng, vị thế, lệnh, giao dịch), tài khoản thật ở chế độ `signal` (chỉ đọc), báo cáo gần nhất; nút **Chạy ngay** như `/chay`.
- **Phân tích** (`/phantich`): **Xem nhanh** như `/nhanh`; **Phân tích bằng AI** như `/phantich`; hàng đợi và nhà cung cấp AI.
- Nút mặt trời/mặt trăng trên thanh đầu trang đổi giao diện sáng/tối. Mọi giờ hiển thị là giờ Việt Nam; ngày của nến là ngày UTC.

### Lịch chạy

- Mỗi ngày lúc `TRADE_TIME` (giờ Việt Nam, mặc định **07:10**; **không bao giờ trước 07:05** = 00:05 UTC, khi nến ngày vừa đóng đã chốt), kể cả cuối tuần và ngày lễ (crypto giao dịch 24/7).
- Mỗi lần chạy: khớp lệnh của nến trước ở giá mở cửa (đã cộng trượt giá và phí), kiểm tra stop/target, ghi NAV cạnh giá BTC, hỏi chiến lược mục tiêu tỷ trọng mới và đặt lệnh cho nến kế tiếp. Báo cáo gửi qua Telegram.
- **Chạy bù:** mỗi lần chạy (kể cả khi server khởi động) bot xử lý lần lượt mọi ngày bị lỡ, mỗi ngày chỉ dùng dữ liệu tới ngày đó. Bù tối đa 30 ngày; ngày cũ hơn bị bỏ qua và lệnh chờ của chúng bị huỷ (báo cáo có ghi).
- **Lỗi dữ liệu:** bot thử lại sau 10, 20, 40, 60... phút (tối đa 6 lần/ngày), báo Telegram một lần; ngày chưa xử lý được chạy bù lần sau. Nguồn chưa có nến mới thì thử lại mỗi 10 phút (tối đa 2 giờ).
- **Không chạy trùng:** lịch, `/chay` và nút web dùng chung một khoá; mỗi tài khoản ảo ghi lại ngày cuối đã xử lý nên không khớp/đặt lệnh hai lần. Các file trạng thái được ghi nguyên tử.

### Chiến lược và kết quả - đọc kỹ

- Hiện có ba tài khoản ảo:
  - **BTC theo 7 đường SMA** (`btc_sma_ens`, chiến lược chính, mặc định cho tín hiệu thật): tỷ trọng BTC = số đường SMA 50, 75, 100, 125, 150, 175, 200 ngày mà giá đóng cửa đang nằm trên, chia 7; phần còn lại giữ USDT. Chỉ đổi tỷ trọng khi lệch mục tiêu quá 10% hoặc khi chuyển giữa 0 và có vị thế. Đây là chiến lược người kiểm định độc lập chọn theo quy tắc dự phòng đã đăng ký trước (không ứng viên nào qua quy tắc chặt). Trên giai đoạn TEST (01/01/2024–14/10/2026, chạy một lần, sau phí): CAGR +25,4%/năm, sụt giảm tối đa −29,6%, Sharpe 0,86; BTC mua và giữ: +24,7%, −53,0%, 0,70. Phần vượt BTC **không có ý nghĩa thống kê**; lợi thế chính là sụt giảm thấp hơn, và nó thua BTC rất xa trong năm tăng mạnh (2024: khoảng +70% so với +112–121%). Chi tiết: `README.md` và `research/RESULTS.md` của dự án.
  - **Xu hướng BTC (SMA200)** (`btc_trend`): mốc so sánh, chiến lược giữ chỗ cũ (giữ BTC khi giá trên SMA200).
  - **Mua & giữ BTC** (`btc_buy_hold`): mốc so sánh.
- Thêm chiến lược: một dòng trong `bot/books.py` trỏ tới tên trong `cryptotrader.strategies.REGISTRY`; khi đó có thể đặt `SIGNAL_STRATEGY=<tên>`.
- Báo cáo hằng ngày có thêm một dòng giải thích tín hiệu, ví dụ "giá trên 4/7 đường SMA → BTC lý tưởng 57% NAV; đang 62%, lệch không quá 10% nên giữ nguyên".
- Lợi nhuận tài khoản ảo **đã trừ** phí taker 0.1% mỗi lệnh và trượt giá giả định 0.1% (đổi bằng `CRYPTO_TAKER_FEE_PCT`, `CRYPTO_SLIPPAGE_PCT`). Phí thật của bạn có thể khác (giảm phí BNB, VIP, lệnh maker).
- Kết quả tài khoản ảo là kết quả **trên dữ liệu mới, sau ngày bắt đầu chạy**, nên trung thực hơn mọi backtest; nhưng vài tháng vẫn là mẫu nhỏ.

### Chế độ tín hiệu cho tài khoản thật

1. Để `TRADE_MODE=paper` vài tháng, theo dõi NAV các tài khoản ảo so với BTC.
2. Muốn nhận lệnh cho tài khoản thật: đặt `TRADE_MODE=signal`, chọn `SIGNAL_STRATEGY`, khởi động lại.
3. Gửi `/von <số USDT trên sàn>`.
4. Mỗi sáng sau `TRADE_TIME` bot gửi **lệnh đề xuất**: coin, khối lượng đã làm tròn theo bước khối lượng (stepSize) của cặp, giá trị ước tính theo giá đóng cửa; lệnh dưới giá trị tối thiểu của sàn (minNotional) được ghi "Bỏ qua" kèm lý do.
5. Lệnh đề xuất áp dụng **đúng quy tắc biên 10% của chiến lược lên tài khoản thật của bạn**: nếu tỷ trọng BTC thật chỉ lệch mục tiêu lý tưởng không quá 10% thì bot báo "giữ nguyên", không đề xuất lệnh nhỏ tốn phí.
6. Tự đặt lệnh trên Binance, rồi báo `/khop` để bot cập nhật sổ tài khoản thật. Bot không bao giờ tự sửa sổ này trong lúc chạy.
7. Đổi `SIGNAL_STRATEGY` khi tài khoản thật đang giữ coin: báo cáo kế tiếp ghi rõ chiến lược đã đổi, và lệnh đề xuất đưa tài khoản về mục tiêu của chiến lược mới.

## 7. Hàng đợi phân tích và bảo vệ

- Mỗi lúc chạy **một** phân tích AI; yêu cầu khác xếp hàng. Yêu cầu **trùng coin và ngày** được gộp vào lượt đang chờ/đang chạy.
- Web + Telegram có tối đa `MAX_PENDING` (mặc định 10) lượt chờ; vượt thì báo "Hàng đợi đầy".
- Khi chưa có phần phân tích AI hoặc chưa có key, `/phantich` trả số liệu nhanh kèm lý do, không xếp hàng.
- Trang web **bắt buộc mật khẩu**; sai mật khẩu nhiều lần thì bị khoá vài phút (trình duyệt đã đăng nhập đúng trước đó được nhớ bằng cookie nên không bị người khác khoá lây; sau reverse proxy, đặt `TRUSTED_PROXY_IPS` để khoá theo IP thật của người truy cập); form gửi từ trang web khác bị từ chối (chống CSRF); trang không cho nhúng vào iframe; trang lỗi không lộ chi tiết lỗi.
- Bot Telegram chỉ phục vụ id trong `TELEGRAM_ALLOWED_IDS`; người lạ chỉ được trả lời tối đa một lần mỗi 5 phút và không chiếm chỗ lệnh của bạn. Mỗi lúc tối đa 3 luồng xử lý lệnh, 50 lệnh chờ (vượt thì bot báo lại cho bạn). Token bot bị xoá khỏi log và tin nhắn lỗi.

## 8. Tầng AI dự phòng

Mỗi lượt gọi AI đi theo `PROVIDER_ORDER` (mặc định `router9,groq,openrouter,xai`; nhà cung cấp chưa có key tự bỏ qua):

- Lỗi kết nối, timeout, lỗi máy chủ → chuyển ngay sang nhà cung cấp kế tiếp.
- Bị 429 (hết quota) → nghỉ theo `Retry-After`, hoặc `LLM_QUOTA_COOLDOWN_SEC` (mặc định 300 s). 9Router lỗi → nghỉ `ROUTER9_RETRY_SEC` (mặc định 900 s).
- **Giới hạn gọi để không quá tải gateway:** mọi lượt gọi đi qua một cổng chung: mỗi lúc một lượt (`LLM_MAX_CONCURRENT=1`), nghỉ ngẫu nhiên 10–20 s sau mỗi lượt (`LLM_MIN_INTERVAL_SEC`, `LLM_MAX_INTERVAL_SEC`), tối đa `LLM_MAX_CALLS_PER_MINUTE` (20) lượt/phút (vượt thì chờ, tối đa `LLM_MAX_WAIT_SEC`), `LLM_MAX_CALLS_PER_DAY` (300) lượt/ngày và `LLM_MAX_CALLS_PER_ANALYSIS` (10) lượt mỗi `/phantich` (vượt thì từ chối: phân tích vẫn xong với quyết định hệ thống). Phân tích gọn `CRYPTO_AGENTS_PROFILE=lite` (mặc định) dùng khoảng 9 lượt, mất 3–5 phút. `/trangthai` hiện số lượt đã dùng.

## 9. Toàn bộ biến môi trường

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `WEB_PASSWORD` | - | **Bắt buộc** để mở trang web |
| `TRUSTED_PROXY_IPS` | `127.0.0.1` | IP của reverse proxy được tin header `X-Forwarded-For` (để khoá đăng nhập theo IP thật). Chỉ đặt `*` khi trang web chỉ truy cập được qua proxy |
| `PUBLIC_URL` | - | Địa chỉ web, để bot gửi link báo cáo |
| `SERVER_PORT` / `PORT` | 8080 | Cổng web |
| `TELEGRAM_TOKEN` | - | Token bot (bỏ trống = tắt bot Telegram) |
| `TELEGRAM_ALLOWED_IDS` | - | Id được dùng bot |
| `TELEGRAM_REPORT_CHAT_IDS` | = allowed ids | Nơi nhận báo cáo hằng ngày |
| `TRADE_MODE` | `paper` | `paper` / `signal` / `off` |
| `SIGNAL_STRATEGY` | `btc_sma_ens` | Tài khoản ảo mà tín hiệu thật đi theo (tên trong `bot/books.py`); cũng là chiến lược quyết định hành động hệ thống trong `/phantich` |
| `PAPER_CAPITAL` | 10000 | Vốn mỗi tài khoản ảo (USDT, tối thiểu 100) |
| `TRADE_TIME` | `07:10` | Giờ bot chạy (HH:MM, giờ VN); giá trị trước 07:05 được nâng lên 07:05 |
| `CRYPTO_TAKER_FEE_PCT` | 0.1 | Phí giả định mỗi lệnh khớp (%) |
| `CRYPTO_SLIPPAGE_PCT` | 0.1 | Trượt giá giả định mỗi lệnh khớp (%) |
| `CRYPTO_DATA_DIR` (hoặc `DATA_DIR`) | `data` | Thư mục dữ liệu (cache nến, sổ giao dịch, báo cáo, hàng đợi) |
| `CRYPTO_BINANCE_DATA_URL` | `https://data-api.binance.vision` | Nguồn nến Binance |
| `CRYPTO_OKX_URL` | `https://www.okx.com` | Nguồn dự phòng |
| `MAX_PENDING` | 10 | Số lượt phân tích AI chờ tối đa |
| `KEEP_REPORTS` | 60 | Số thư mục báo cáo AI giữ lại |
| `PROVIDER_ORDER` | `router9,groq,openrouter,xai` | Thứ tự dự phòng AI |
| `ROUTER9_BASE_URL`, `ROUTER9_API_KEY`, `ROUTER9_MODEL` | - | 9Router |
| `GROQ_API_KEY`, `GROQ_MODEL` | - | Groq |
| `OPENROUTER_API_KEY`, `OPENROUTER_MODEL` | - | OpenRouter |
| `XAI_API_KEY`, `XAI_MODEL` | - | Grok (tuỳ chọn) |
| `LLM_QUOTA_COOLDOWN_SEC`, `ROUTER9_RETRY_SEC`, `LLM_CALL_TIMEOUT_SEC` | 300 / 900 / 120 | Nghỉ khi 429, khi 9Router lỗi; timeout mỗi lượt gọi |
| `LLM_MAX_CONCURRENT` | 1 | Số lượt gọi AI chạy cùng lúc (1 = tuần tự) |
| `LLM_MIN_INTERVAL_SEC`, `LLM_MAX_INTERVAL_SEC` | 10 / 20 | Nghỉ ngẫu nhiên trong khoảng này (giây) sau mỗi lượt gọi |
| `CRYPTO_AGENTS_PROFILE` | `lite` | `lite`: phân tích gọn ~9 lượt gọi (không tranh luận, không nhóm rủi ro); `full`: đầy đủ 18–35 lượt |
| `LLM_MAX_CALLS_PER_MINUTE`, `LLM_MAX_CALLS_PER_HOUR` | 20 / 0 | Tối đa lượt gọi trong 60 s / 1 giờ bất kỳ; vượt thì chờ (0 = không giới hạn) |
| `LLM_MAX_WAIT_SEC` | 300 | Chờ lâu hơn thế thì bỏ lượt gọi |
| `LLM_MAX_CALLS_PER_DAY`, `LLM_MAX_CALLS_PER_ANALYSIS` | 300 / 10 | Ngân sách mỗi ngày (giờ VN) và mỗi lượt phân tích; vượt thì từ chối (0 = không giới hạn) |
| `FUTURES_MODE` | `paper` | Futures long/short: `paper` / `signal` / `off` |
| `FUTURES_SYMBOLS` | `ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,BNBUSDT` | Cặp futures, mỗi cặp một sổ ảo (cặp phải có trên Binance spot) |
| `FUTURES_SIGNAL_SYMBOLS` | `BNBUSDT` | Cặp cho tín hiệu tài khoản thật, nhiều cặp thì vốn chia đều. Với 10 USDT chỉ BNB chạy đúng cỡ (ETH cần ≥ 43, BTC ≥ 72 USDT) |
| `FUTURES_DD_SCALE` | 0 | Giảm cỡ vị thế khi sụt giảm (0.3 = còn 25% cỡ khi sụt 30%); cắt nửa sụt giảm và nửa lãi, vô tác dụng với vốn ~10 USDT |
| `FUTURES_RULE` | `v2` | Bộ tham số `ls_sma`: `v2` (đã tối ưu walk-forward) hoặc `v1` (bản đầu) |
| `FUTURES_CAPITAL` | 10 | Vốn mỗi sổ futures ảo (USDT) |
| `FUTURES_GOAL_PROFIT` | 100 | Đạt vốn + số này: đóng vị thế và dừng (0 = không có mục tiêu) |
| `FUTURES_STOP_EQUITY` | nửa vốn (5) | Vốn còn bằng hoặc dưới số này: đóng vị thế và dừng (0 = không dừng) |
| `FUTURES_TARGET_VOL`, `FUTURES_MAX_LEVERAGE` | theo `FUTURES_RULE` (v2: 0.3 / 2) | Ghi đè biến động mục tiêu và đòn bẩy tối đa |
| `FUTURES_MIN_ORDER_LEVERAGE` | 3 | Đòn bẩy cao nhất chấp nhận để đạt lệnh tối thiểu của sàn |
| `FUTURES_FUNDING_PCT_DAY`, `CRYPTO_FUT_TAKER_FEE_PCT`, `CRYPTO_FUT_SLIPPAGE_PCT` | 0.03 / 0.05 / 0.05 | Funding giả định cho lệnh long (%/ngày); phí và trượt giá futures (% mỗi lệnh) |

## 10. Xử lý sự cố

| Triệu chứng | Cách xử lý |
|---|---|
| Trang web báo "Trang web đang khoá" | Đặt `WEB_PASSWORD`, khởi động lại |
| Bot trả "Bot chưa cho phép tài khoản này" | Thêm id vào `TELEGRAM_ALLOWED_IDS`, khởi động lại |
| `/phantich` báo "Phân tích AI chưa dùng được" | Đọc lý do: thiếu phần phân tích AI trong gói, hoặc chưa có key AI. `/nhanh` vẫn dùng được |
| `/nhanh` báo lỗi kết nối | Máy chủ không tới được `data-api.binance.vision` và OKX; kiểm tra mạng của host |
| Báo cáo "Chưa lấy được nến ..." | Nguồn dữ liệu chậm; lần chạy sau sẽ chạy bù, hoặc gửi `/chay` |
| `/khop` báo sổ tài khoản thật bị hỏng | File `data/trading/real.json` hỏng đã được chuyển sang `real.json.corrupt-...` (không xoá). Đổi tên lại thành `real.json` sau khi sửa, hoặc xoá file đó để bắt đầu sổ mới |
| Lệnh đề xuất "Bỏ qua ... dưới mức tối thiểu" | Vốn quá nhỏ cho giá trị lệnh tối thiểu của sàn (thường 5-10 USDT) |
| Link báo cáo trong Telegram thiếu | Đặt `PUBLIC_URL` |

> Thông tin chỉ mang tính tham khảo, không phải khuyến nghị đầu tư. Crypto biến động rất mạnh; chỉ dùng số tiền bạn chấp nhận mất.
