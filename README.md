# AI Movie Review Video Editor (bản desktop)

Ứng dụng desktop: bấm đôi `MovieReviewEditor.exe` là mở **cửa sổ ứng dụng riêng** (Microsoft Edge WebView2) — không mở
trình duyệt, không có cửa sổ đen. Máy chủ nội bộ chạy ngầm và tắt cùng cửa sổ. Dữ liệu (SQLite, file upload, video xuất)
nằm trong `%USERPROFILE%\MovieReviewEditor`. Claude chỉ trả JSON edit plan; FFmpeg ở máy bạn mới dựng video.

## Mở ứng dụng (từ v1.1.0)
1. **Màn hình khoá:** nhập **key bảo mật + mật khẩu**. Lần đầu mở thì tự tạo (≥ 6 ký tự mỗi thứ, lưu dạng băm ở
   `auth.json`); các lần sau phải nhập đúng cả hai. Sai 5 lần bị khoá tạm (30 giây, tăng dần). Mọi API cục bộ đều bị
   chặn khi chưa đăng nhập. Đổi key/mật khẩu hoặc **🔒 Khoá** ở ⚙ Cài đặt / thanh trên.
   *Người phát hành muốn tự cấp key cho mọi máy:* chạy `python scripts/make_access.py` trong `backend` để tạo `access.json`
   rồi build lại — bản exe đó chỉ nhận đúng key + mật khẩu bạn cấp.
   *Quên key/mật khẩu (kiểu tự tạo):* đóng app, xoá `%USERPROFILE%\MovieReviewEditor\auth.json`, mở lại để tạo mới (dự án và key API vẫn giữ).
2. **Vào thẳng giao diện tool.** Chưa có API key thì app tự mở ⚙ Cài đặt: **dán API key là tự kết nối** (tự kiểm tra,
   tự nhận kiểu API/model, lưu khi thành công) — không cần bấm nút. Trạng thái hiện ở nút "API" trên thanh trên.
3. Máy thiếu WebView2 (hiếm, Windows 10 cũ): bộ cài tự cài; bản portable thì app mở bằng Edge chế độ ứng dụng.
   Chạy `MovieReviewEditor.exe --browser` (hoặc `python launcher.py --browser`) nếu muốn dùng trình duyệt như trước.

## Build thành .exe (Windows)
**Xem `HUONG_DAN_BUILD.md`** (3 cách: build trên cloud bằng GitHub Actions không cần cài Python; `build_exe.bat` trên máy; hoặc `run_dev.bat` không cần exe). `build_exe.bat` ghi log vào `build_log.txt`, tự chạy thử file exe bằng `--selftest`, có kiểu NHẸ (không Whisper) để dễ build thành công.
(Phần bên dưới là hướng dẫn cũ.)

1. Cài Python 3.10–3.12 (tick "Add to PATH").
2. Chạy `build_exe.bat` (tự cài thư viện, tải FFmpeg, chạy PyInstaller).
3. Kết quả: `backend\dist\MovieReviewEditor\MovieReviewEditor.exe`. Nén cả thư mục `MovieReviewEditor` để gửi người khác.

Lưu ý: bản có Whisper nặng khoảng 1 GB. Muốn nhẹ hơn, xoá `requirements-ai.txt` khỏi `build_exe.bat`
(mất Whisper; scene detection chuyển sang cắt cố định 6 giây). Lần đầu dùng Whisper sẽ tải model từ Internet.
Không build chéo được: phải build trên Windows để ra file .exe.

## Chạy thử không build
`run_dev.bat` (Windows) hoặc `./run_dev.sh` (Linux/macOS, cần ffmpeg trong PATH).

## Bản sửa v1.7.1

- **Phụ đề in vào video bị to và nằm giữa màn hình** → đã sửa: chữ đúng cỡ, nằm sát đáy, giống bản xem nhanh.
- **⚡ Tự động tạo video / Tự động edit** chạy trọn gói tới **video thành phẩm 1080p** (lưu vào thư mục đầu ra), xong tự phát bản mới:
  - Có footage phim: phân tích cảnh → bản dựng → xuất video.
  - Không có footage (chỉ kịch bản hoặc SRT + voice MP3): tự tìm ảnh/video miễn phí bản quyền → dựng khớp giọng → xuất video.
- Bỏ qua file voice/footage bị lỗi khi dựng; không tự chạy quy trình nặng khi mới thêm file mà chưa có footage.

## Mới trong v1.7.0 — Sửa đoạn chưa ưng + DeepSeek kiểm chứng kịch bản

- **📍 Sửa đoạn này** (thanh phát video): xem tới đoạn chưa ưng → bấm → gõ điều muốn sửa vào Trợ lý AI. Claude biết chính xác câu phụ đề và cảnh ở thời điểm đó, sửa bản dựng rồi **tự dựng lại video ngầm** (cùng chất lượng bản đang xem). Xong app tự phát bản mới, quay về đúng đoạn vừa sửa.
- Đang dựng vẫn chat được: yêu cầu mới được ghi nhớ và dựng lại ngay khi lần dựng hiện tại xong.
- **🔎 DeepSeek — kiểm tra kịch bản & kiểm chứng thông tin**: mục riêng trong ⚙ Cài đặt, dán API key DeepSeek là tự kết nối (giống Claude). Dùng ở:
  - Kịch bản → Video: nút “🔎 Kiểm tra kịch bản” (đánh dấu Sai / Chưa chắc / Nên nêu nguồn, thay từng câu hoặc dùng cả bản đã sửa). Khi tạo video tự động, kịch bản AI viết được DeepSeek soát và tự sửa.
  - Kịch bản (phụ đề): “🔎 Kiểm tra phụ đề” → “Nhờ trợ lý Claude sửa phụ đề theo đề xuất”.
  - Hiện số dư DeepSeek (USD/CNY) và chi phí ước tính.
  - Lưu ý: DeepSeek đánh giá theo hiểu biết của model, không tự tra cứu web.

## Mới trong v1.6.0 — Kịch bản → Video (tự tìm hình minh hoạ)
Menu **🎬 Kịch bản → Video**: chọn phong cách, chọn hình minh hoạ **Ảnh / Video / Kết hợp**, nhập **chủ đề** (AI viết
kịch bản) hoặc **dán kịch bản** → **⚡ Tạo video tự động**:
1. Giọng đọc AI (ElevenLabs/OpenAI) + **phụ đề SRT khớp từng câu** (hoặc dùng voice-over bạn tải lên / tách từ video người dẫn).
2. Claude đọc từng câu → chọn từ khoá + ảnh hay video → app tìm & tải từ kho **miễn phí bản quyền, dùng thương mại được**:
   **Pexels**, **Pixabay** (cần API key miễn phí, nhập ngay trong menu) và **Wikimedia Commons** (ảnh tư liệu, không cần key).
   Không lấy ảnh Google/YouTube (bản quyền → dễ bị gậy). Ảnh Wikimedia cần ghi nguồn: nút **📄 Nguồn hình** sao chép sẵn.
3. Lắp bản dựng theo phong cách (nhịp cắt, khung 16:9/9:16, zoom Ken Burns cho ảnh, chuyển cảnh, màu, cỡ phụ đề) → xem thử.
   Sau đó chỉnh bằng 🤖 Chat AI như mọi bản dựng.

**19 phong cách**: 📰 Tin tức (người dẫn, tin ngắn dọc, phóng sự hiện trường, tổng hợp tin, đồ hoạ giải thích, livestream*),
📜 Lịch sử (tài liệu, tái hiện, hoạt hình, “Bạn có biết…”, tư liệu cũ, kể chuyện), 💰 Tài chính (giảng giải, faceless,
hoạt hình, tạp kỹ/phỏng vấn, tin ngắn, hướng dẫn, MV).
- Phong cách cần người thật (người dẫn, phỏng vấn, hiện trường, tái hiện, tạp kỹ): app không tạo người thật; tải video
  bạn tự quay (loại Footage) → app tách giọng, xen kẽ cảnh người dẫn **đúng thời điểm nói** với hình minh hoạ.
- *Livestream là phát trực tiếp, không phải dựng video → không hỗ trợ.
- Hoạt hình / đồ hoạ: dùng hình minh hoạ (illustration) có sẵn trên kho + hiệu ứng chuyển động, không tự vẽ hoạt hình.
- Ảnh/video tải về được cắt ngắn, bỏ tiếng; dựng lại thì hình không dùng nữa tự xoá → không tốn ổ đĩa.

## Mới trong v1.5.0
- **Tài khoản**: màn hình khoá có 2 tab **Đăng nhập | Đăng ký**. Đăng ký gửi yêu cầu lên tool Admin → Admin duyệt và gửi
  **mã kích hoạt** (`XXXX-XXXX`) → nhập vào app. Máy đã kích hoạt đăng nhập không cần mạng; đăng nhập tài khoản trên
  máy khác mới cần kích hoạt lại (máy cũ tự bị thu hồi). Đổi mật khẩu trong ⚙ Cài đặt áp dụng cho mọi máy;
  quên mật khẩu thì Admin đặt mật khẩu mới. Chi tiết: `HUONG_DAN_ADMIN.md`.

## Mới trong v1.4.0
- **Giấy phép theo mã máy + tool Admin** (xem `HUONG_DAN_ADMIN.md`): mã máy tính từ phần cứng, lần đầu mở app tự gửi về
  máy chủ Admin; Admin duyệt, đặt hạn dùng, gia hạn, khoá máy, cho đặt lại mật khẩu. Giấy phép ký số Ed25519 → kiểm tra
  được khi không có mạng. Bản build không gắn `LICENSE_PUBKEY` thì không yêu cầu giấy phép.
- **Đăng nhập = mã máy (tự điền) + mật khẩu.** Đổi mật khẩu ở ⚙ Cài đặt (bản Setup và Portable như nhau).
- **Portable có sẵn FFmpeg** — không phải cài thêm gì.
- **Thư mục làm việc** (thay cho mục Dung lượng ổ đĩa): thư mục ĐẦU VÀO (hộp chọn file mở sẵn ở đây; mục Tài nguyên có nút
  quét nhanh để thêm file, không sao chép) và thư mục ĐẦU RA (video xuất 1080p lưu thẳng vào đây, có nút mở thư mục).
- **Thanh cấu hình máy ở dưới cùng**: CPU %, RAM, GPU (tải %, VRAM, nhiệt độ), bộ mã hoá đang dùng, số phiên NVENC,
  tốc độ đọc/ghi ổ đĩa, dung lượng trống. Bấm vào để đổi **chế độ hiệu năng**:
  - *GPU ưu tiên* (mặc định): luôn mã hoá bằng GPU (NVENC / Quick Sync / AMF) nếu có; FFmpeg chạy ưu tiên thấp và chừa lõi
    CPU → máy không bị đơ khi đang xuất. Số đoạn dựng song song tự chọn theo GPU, số lõi và RAM.
  - *Tối đa tốc độ*: dùng hết sức máy. *Tiết kiệm*: 1 tiến trình, ít luồng — cho laptop yếu.

## Gửi app cho máy khác & cập nhật online (v1.3.0)
**Gửi cho máy khác — chọn 1 file:**
- `MovieReviewEditor-Setup-X.Y.Z.exe` (**nên dùng**): bấm đôi là tự cài (không hỏi gì, không cần Admin), tạo icon Desktop và
  mở app ngay. Tự cài kèm WebView2 / VC++ nếu máy thiếu. Hỗ trợ **tự cập nhật** đầy đủ.
- `MovieReviewEditor-Portable-X.Y.Z.exe`: 1 file chạy ngay, không cần cài, chép đi đâu cũng được. Bản nhẹ (không Whisper,
  không OpenCV; FFmpeg tự tải lần đầu cần dùng). Mỗi lần mở chậm hơn vài giây vì phải giải nén tạm.
- ⚠ **Không** gửi riêng file `MovieReviewEditor.exe` trong `backend\dist\MovieReviewEditor\` — nó cần thư mục `_internal`
  đi kèm, thiếu là báo lỗi *Failed to load Python DLL … python312.dll*.

**Cập nhật online:** ⚙ Cài đặt → *Cập nhật phiên bản* (hoặc nút tím **⬆ Cập nhật vX** trên thanh trên khi có bản mới) →
*Tải* → *Cài & mở lại*. Bản Setup cài đè im lặng; bản Portable tự thay file exe của nó. Dự án, key, cài đặt giữ nguyên.
File tải được kiểm tra dung lượng + mã SHA-256 (GitHub cung cấp), và tự xoá sau khi cài.

**Người phát hành — đăng bản mới:**
1. Repo GitHub để **Public** (máy khác mới đọc được mục Releases mà không cần mật khẩu).
2. Sửa code, đổi số trong `backend/app/version.py`, rồi đẩy tag: `git tag v1.3.1` → `git push origin v1.3.1`.
3. GitHub Actions tự build Setup + Portable và đăng lên **Releases**. Mọi máy đang dùng app sẽ thấy nút cập nhật
   (tự kiểm tra khi mở app, tắt được trong Cài đặt). Nguồn cập nhật mặc định là repo đã build ra app; đổi được trong
   Cài đặt (dạng `chu/repo` hoặc URL tới file JSON `{"version","notes","setup_url","portable_url","setup_sha256"}`).
   Build trên máy bằng `build_exe.bat`: đặt `set MRE_UPDATE_REPO=chu/repo` trước khi chạy để gắn sẵn nguồn.

## Tính năng mới v1.2.0
- **Tín dụng API trên thanh trên** (💳 số dư · 🎙 ký tự ElevenLabs), chi tiết từng nguồn ở ⚙ Cài đặt:
  - Relay kiểu one-api / new-api (đa số relay): đọc số dư thật qua `/dashboard/billing/*` hoặc `/api/usage/token`.
  - OpenRouter: đọc `/api/v1/credits`. ElevenLabs: số ký tự còn lại (cộng dồn các key).
  - **Anthropic chính thức không có API xem số dư** → app tự đếm token của mọi lần gọi và ước tính chi phí; đặt
    **"Hạn mức tự đặt (USD)"** cho nguồn để app hiện "còn ~$x". Số liệu tự làm mới sau mỗi tác vụ AI và mỗi 5 phút.
- **🤖 Trợ lý AI (khung chat bên phải, nút 💬 Chat AI)**: ra lệnh bằng lời ("hook mạnh hơn", "đổi cảnh câu 5 sang cận mặt",
  "đen trắng cả video", "phụ đề to hơn", "dựng xem thử"). Claude trả về các thao tác nhỏ (không gửi lại cả bản dựng → ít token),
  app tự sửa lỗi, kiểm tra và lưu thành **phiên bản mới** (quay lại bản cũ ở mục Bản dựng). Nút **⚡ Tự động edit**: thiếu tài
  nguyên thì AI bổ sung, đủ rồi thì AI tối ưu toàn bộ bản dựng.
- **Tự phân tích & vá chỗ thiếu bằng AI** (mục Tài nguyên → "Tình trạng dự án"): mỗi khi thêm file, app tự phân tích cảnh rồi
  bổ sung theo thứ tự: AI viết kịch bản (theo tên phim + cảnh nhận diện được) → AI đọc giọng (ElevenLabs/OpenAI TTS) → phụ đề
  (từ giọng AI, Whisper, hoặc ước lượng theo kịch bản) → Claude lên bản dựng → tiêu đề/mô tả/hashtag. Footage phim thì AI không
  tạo được; nhạc nền/logo là tuỳ chọn. Tắt bằng ô "Tự phân tích & bổ sung bằng AI mỗi khi thêm file". Không tự xuất video.
- **Tiết kiệm ổ đĩa**:
  - Bản desktop có nút **📂 Chọn file trên máy (không sao chép)**: footage dùng tại chỗ, không nhân đôi dung lượng
    (kéo thả vẫn sao chép như cũ). Xoá tài nguyên liên kết **không bao giờ** xoá file gốc.
  - Bộ nhớ đệm dựng video giới hạn **2 GB** (chỉnh 1–8 GB), giọng đọc 512 MB, ảnh xem trước 256 MB; tự dọn khi vượt.
  - Chỉ giữ **bản xem thử mới nhất** mỗi dự án (bản 1080p giữ nguyên); giọng AI cũ bị thay thì tự xoá.
  - ⚙ Cài đặt → **Dung lượng ổ đĩa**: xem từng phần đang chiếm bao nhiêu và nút **🧹 Dọn**.

## Giao diện (3 ô)
- **Kết nối API (⚙ Cài đặt):** dán **API key** (và **Base URL** nếu dùng relay; để trống nếu dùng Anthropic chính thức) — app **tự kết nối** ngay khi dán xong. Kiểu API để "Tự nhận diện": app thử Anthropic (`/v1/messages`) rồi OpenAI-compatible (`/chat/completions`), lấy danh sách model của nguồn và tự chọn model phù hợp; chỉ lưu khi gọi thử thành công.
- **Nhiều nguồn kết nối (Cài đặt):** thêm nhiều cặp Base URL + API key (mỗi nguồn có kiểu API và model riêng), chọn nguồn đang dùng, bật/tắt từng nguồn, kiểm tra riêng từng nguồn. Nếu nguồn đang dùng lỗi (quá tải, mất kết nối, hết quota...), app tự thử các nguồn còn lại theo thứ tự; lỗi 400 (yêu cầu sai) thì không thử nguồn khác.
- Nguồn OpenAI-compatible cần hỗ trợ function calling (tool) cho model được chọn, vì bản dựng và gắn nhãn cảnh dùng tool.
- **Mọi dịch vụ gọi API đều có nhiều nguồn riêng** (Cài đặt, ba khối độc lập, mỗi khối có nguồn đang chọn / bật-tắt / kiểm tra riêng):
  - **Claude** (bản dựng, gắn nhãn cảnh, tiêu đề): Base URL + key + kiểu API + model riêng cho từng nguồn.
  - **ElevenLabs** (giọng đọc): mỗi key một Base URL. Quota được **cộng dồn** qua các key; key nào hết quota hoặc lỗi (401/429/5xx/mất mạng) thì đoạn đó tự được tạo bằng key kế tiếp. Lỗi yêu cầu (400/404/422, vd sai voice_id) không thử key khác để khỏi tốn credit. Giọng có sẵn của ElevenLabs dùng được ở mọi key; giọng tự nhân bản chỉ có ở tài khoản chủ.
  - **OpenAI TTS** (giọng dự phòng): tương tự.
  - Key đơn của bản cũ hoặc biến môi trường tự hiện thành một nguồn "mặc định".
- **Ô trái:** menu Dự án · Tài nguyên (kéo thả file, tự nhận diện loại) · Kịch bản · Giọng AI · Bản dựng · Xuất video · Cài đặt.
- **Ô giữa:** màn xem video đang dựng (xem nhanh theo bản dựng, chạy trực tiếp từ footage + giọng đọc, không cần render; hoặc xem video đã xuất) và timeline 6 track: giọng đọc, phụ đề, cảnh phim (có ảnh xem trước, kéo thả đổi chỗ), hiệu ứng, nhạc, logo. Phím tắt: Space phát/dừng, ←/→ tua 1 giây.
- **Ô phải (tab):** Clip · Phụ đề · Hiệu ứng · Âm thanh · Logo · Nội dung (tiêu đề/mô tả/hashtag). Sửa xong bấm **Lưu & kiểm tra**: hệ thống tự co giãn clip cho khớp lời rồi kiểm tra hợp lệ.
- Nút **⚡ Tự động tạo video** ở thanh trên: phân tích cảnh → phụ đề (Whisper nếu chưa có) → Claude lên bản dựng → dựng video xem thử.
- Xem nhanh chỉ là bản gần đúng (màu/bộ lọc xấp xỉ bằng CSS). Footage trình duyệt không phát được (vd HEVC/MKV) sẽ hiện ảnh tĩnh thay thế; video xuất ra vẫn đúng.

## Cách dùng
1. ⚙ Cài đặt: nhập Anthropic API key.
2. Tạo project, upload footage, voice-over, (logo, nhạc, LUT).
3. Bấm **Phân tích footage** (PySceneDetect; tuỳ chọn gắn nhãn cảnh bằng Claude vision để chọn đúng close-up/wide).
4. Nạp SRT, hoặc bấm **Tạo phụ đề từ voice-over** (Whisper). Sửa cue nếu cần.
5. **Generate Edit Plan** → xem storyboard, sửa JSON nếu cần, Validate. Plan lỗi sẽ được gửi lại Claude tối đa 3 lần.
6. Thẻ **Tiêu đề, mô tả, hashtag, thumbnail** nhờ Claude gợi ý nội dung đăng YouTube.
7. **Render** preview 720p, ưng thì render final 1080p. Tải MP4, SRT, plan JSON.

## Tối ưu hiệu năng
- **Cache đoạn video:** mỗi đoạn được lưu theo hash nội dung (footage, thông số clip, độ phân giải, fps...). Sửa một cue rồi render lại chỉ dựng lại đoạn đổi; cache tự dọn khi vượt 8 GB (LRU) ở `~/MovieReviewEditor/cache/segments`.
- **Dựng song song** 1-3 đoạn cùng lúc tuỳ số lõi CPU (`parallel_segments` trong API để chỉnh).
- **Tự sửa plan trước khi gọi lại Claude** (`repair_service.py`): kẹp giá trị vào giới hạn, đưa clip vào đúng scene, chốt voice time/subtitle theo cue, co giãn clip cuối (kể cả slow-mo) để khớp độ dài cue. Giảm số lần retry, tiết kiệm API.
- **Tăng tốc GPU** (NVENC / Intel QSV / AMD AMF): ffmpeg phải liệt kê encoder VÀ mã hoá thử thành công thì mới dùng; giải mã cũng dùng `-hwaccel auto`. Nếu lỗi giữa chừng, tự chuyển sang CPU cho phần còn lại của lần render. Tắt bằng ô "GPU" trên giao diện.
- **Đoạn trung gian nhẹ hơn:** đoạn cache mã hoá `veryfast`, chất lượng cao; chỉ bước ghép cuối mới mã hoá kỹ, tránh mã hoá nặng hai lần.
- **Phân tích footage song song** (tối đa 3 file cùng lúc); storyboard giới hạn 3 tiến trình trích ảnh đồng thời; giao diện hiện thời gian còn lại ước tính khi render.
- **Claude API:**
  - Một client dùng chung (giữ kết nối), tự retry 429/5xx/529 có backoff và Retry-After; lỗi được dịch ra tiếng Việt (key sai, hết credit, quá tải, model không tồn tại...).
  - Edit plan nhiều đoạn: đoạn 1 chạy trước, các đoạn còn lại chạy **song song** (tối đa 3) với bảng filter của đoạn 1; đoạn nào xung đột cảnh đã dùng với đoạn song song khác thì dựng lại. Khối dữ liệu tĩnh giống hệt nhau giữa các đoạn nên dùng chung prompt cache.
  - Giảm token output: Claude không phải điền `subtitle` và bỏ các trường bằng giá trị mặc định (hệ thống tự điền).
  - Gắn nhãn cảnh bằng model rẻ/nhanh (`claude_fast_model`, mặc định Haiku), trích frame và gọi API song song.
  - Nút "Kiểm tra Claude/ElevenLabs/OpenAI" trong Cài đặt: xác nhận key dùng được và cho biết quota còn lại.
- **ElevenLabs:**
  - Tạo giọng các đoạn **song song** (mặc định 2 request đồng thời cho gói Free; đổi `tts_concurrency` trong settings.json nếu gói cao hơn).
  - **Cache đoạn đã tạo** theo (chữ, giọng, phong cách, tốc độ, model): sửa vài câu rồi tạo lại chỉ tốn credit cho câu đã sửa. Cache TTS tự dọn khi vượt 1 GB.
  - **Kiểm tra quota trước khi tạo** (chỉ tính đoạn chưa có cache) để không tốn credit cho một lần tạo dở dang.
  - Gửi `previous_text`/`next_text` để ngữ điệu liền mạch giữa các đoạn; tự thử lại không có ngữ cảnh nếu model từ chối.
  - Chế độ "Tiết kiệm (flash)": nhanh hơn, ~50% credit, chất lượng thấp hơn. Danh sách giọng được nhớ 10 phút.
- **Giọng đọc đáng tin cậy hơn:**
  - Thiếu FFmpeg thì hiện hộp cảnh báo kèm nút **Tải FFmpeg tự động** (Windows).
  - Model giọng đọc OpenAI-compatible đặt riêng cho từng nguồn; relay không có model mặc định thì tự thử `tts-1`.
  - Relay trả HTTP 200 nhưng nội dung là JSON/HTML báo lỗi được phát hiện và hiện đúng nội dung.
  - Nút **Kiểm tra giọng đọc** chạy thật một câu ngắn và liệt kê từng bước (nguồn, FFmpeg, âm thanh nhận về, giải mã).
- **Huỷ render** từ giao diện; thư mục tạm của lần chạy trước được dọn khi khởi động.

## Kiến trúc
`app/services/claude_service.py` (tool use + retry + chia batch 40 cue) → `validator_service.py` (luật cứng) →
`render_service.py` (FFmpeg: từng đoạn đúng số frame → concat → voice + nhạc ducking + phụ đề + logo).
Job nền chạy bằng thread (`workers/`), render xếp hàng 1 job/lần. Đổi `DATABASE_URL` để dùng PostgreSQL.

## Key cần nhập (⚙ Cài đặt)
Anthropic (bắt buộc cho plan/metadata) · ElevenLabs hoặc OpenAI (chỉ khi dùng Voice AI). Key lưu cục bộ ở `settings.json`, không gửi đi đâu ngoài nhà cung cấp tương ứng.

## Test
`pip install -r requirements-dev.txt && pytest` (trong thư mục `backend`). Test không cần FFmpeg hay API key.

## Chưa làm / hạn chế (so với đặc tả)
- Auth đa người dùng, giới hạn dung lượng theo plan, WebSocket (UI dùng polling 2s).
- Timeline kéo-thả: hiện chỉnh bằng JSON; storyboard chỉ xem.
- Hiệu ứng `shake`, pan, text animation, intro/outro logo, thư viện nhạc: chưa hỗ trợ. `whip_pan` làm bằng blur.
- 9:16 là crop giữa, chưa reframing theo khuôn mặt.
- Chưa có forced alignment cho kịch bản dán tay: phụ đề từ Whisper dùng chữ Whisper nghe được.
- Phụ đề CJK/Việt cần font hệ thống hỗ trợ (đổi `subtitle_font`).
- Footage phim có bản quyền có thể bị Content ID/strike; người dùng tự chịu trách nhiệm về nguồn footage và nhạc.
