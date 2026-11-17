# Gửi tool cho máy khác — cách đúng

**Đừng gửi cả thư mục mã nguồn hay file `.bat`.** Hãy gửi **một file cài đặt** `MovieReviewEditor-Setup-x.y.z.exe`: người nhận bấm đôi → Next → xong (không cần Python, không cần Admin, đã kèm FFmpeg). Hướng dẫn cho người nhận: `HUONG_DAN_CAI_DAT.md`.

## Tạo file cài đặt
- **Trên cloud (không cần cài gì):** làm Cách A bên dưới. Workflow tự tạo cả `Setup.exe` (artifact *MovieReviewEditor-Setup*) và bản zip không cần cài (*MovieReviewEditor-portable*).
  Đẩy tag `v1.0.1` lên GitHub thì file tự xuất hiện ở mục **Releases** — gửi link đó cho mọi người, họ tự tải.
- **Trên máy bạn:** `winget install Python.Python.3.12` và `winget install JRSoftware.InnoSetup`, rồi bấm đôi `build_installer.bat`. Kết quả ở thư mục `installer_output\`.

## Cách chia sẻ
| Cách | Khi nào dùng |
|---|---|
| Link GitHub Releases | Nhiều người, cần cập nhật bản mới (cài đè không mất dữ liệu) |
| Google Drive / OneDrive / USB | Vài máy, không dùng GitHub |
| Bản zip portable | Máy không cho chạy trình cài đặt: giải nén rồi chạy `MovieReviewEditor.exe` |

## Lưu ý quan trọng
- **API key không nằm trong bộ cài** (đúng chủ ý, để không lộ key). Mỗi máy nhập key riêng ở màn hình Kết nối.
- **SmartScreen sẽ cảnh báo** vì file chưa ký số; người nhận bấm *More info → Run anyway*. Muốn hết cảnh báo cần mua chứng chỉ ký mã (Code Signing) rồi ký file Setup.exe.
- **Antivirus** đôi khi báo nhầm file đóng gói bằng PyInstaller; có thể gửi file lên Microsoft/antivirus để báo "false positive".
- Mỗi bản phát hành: sửa số trong `backend/app/version.py` (hoặc đặt tag `vX.Y.Z`).

---

# Tạo file .exe — 3 cách (chọn một)

> File `.exe` chỉ tạo được **trên Windows**. Không có cách nào tạo ra nó trên Mac/Linux hay trong trình duyệt.

## Cách A — Build trên cloud (KHÔNG cần cài Python) ← dễ nhất
1. Tạo tài khoản GitHub (miễn phí) → **New repository** (để Private cũng được).
2. Giải nén zip này, kéo thả TOÀN BỘ nội dung thư mục `movie-review-editor` lên repo (nút *Add file → Upload files*).
   Phải có sẵn thư mục `.github/workflows` (thư mục ẩn: bật "Hiện mục ẩn" khi chọn file).
3. Vào tab **Actions** → **Build Windows exe** → **Run workflow** (chọn `lite` để thử trước, `full` nếu cần Whisper).
4. Đợi 10–20 phút → mở lần chạy đó → mục **Artifacts** → tải `MovieReviewEditor-windows`.
5. Giải nén, chạy `MovieReviewEditor.exe`. Bước "Tự kiểm tra exe" ở workflow chạy thật file exe (máy chủ, đăng nhập, cơ sở dữ liệu, thư viện cửa sổ) nên nếu xanh là exe chạy được.
   *Nếu dùng `access.json` (key cấp sẵn), nhớ đưa cả file `backend/access.json` lên repo.*

## Cách B — Build trên máy bạn
1. Cài Python 3.12: mở CMD gõ `winget install Python.Python.3.12` (hoặc tải ở python.org, **tick "Add python.exe to PATH"**).
   *Không dùng Python 3.13+ và không dùng bản Microsoft Store.*
2. Bấm đôi `build_exe.bat` → chọn **2 (NHẸ)** để thử trước.
3. Xong sẽ có `backend\dist\MovieReviewEditor\MovieReviewEditor.exe` và `MovieReviewEditor-windows.zip`.
4. **Nếu lỗi:** script in 30 dòng cuối và lưu `build_log.txt` — gửi file đó để được sửa.

## Cách C — Không cần exe
Bấm đôi `run_dev.bat` (tự cài thư viện, tự tải FFmpeg, mở cửa sổ ứng dụng giống bản exe).

## Lỗi hay gặp
| Triệu chứng | Nguyên nhân | Cách xử lý |
|---|---|---|
| "Khong tim thay Python" | Chưa cài, hoặc chỉ có bản Microsoft Store / 3.13+ | `winget install Python.Python.3.12` |
| Dừng ở bước 3 (pip) | Mạng chặn PyPI, hoặc Whisper không có wheel cho máy bạn | Chọn kiểu **NHẸ**; hoặc dùng Cách A |
| Bước 5 (PyInstaller) lỗi | Thiếu RAM / antivirus xoá file | Tạm tắt antivirus, đóng bớt chương trình, thử kiểu NHẸ |
| Bấm exe không thấy cửa sổ | Đang có một bản chạy sẵn (app sẽ đưa cửa sổ đó lên trước) hoặc lỗi khởi động | Xem `%USERPROFILE%\MovieReviewEditor\logs\app.log` |
| Cửa sổ trắng / lỗi WebView2 | Máy thiếu Microsoft Edge WebView2 Runtime | Dùng bộ cài Setup.exe (tự cài), hoặc tải "WebView2 Runtime Evergreen" của Microsoft |
| Quên key bảo mật / mật khẩu | — | Đóng app, xoá `%USERPROFILE%\MovieReviewEditor\auth.json`, mở lại để tạo mới |
| Giọng đọc lỗi "thiếu FFmpeg" | Chưa có `ffmpeg.exe` | Trong app: Giọng AI → **Tải FFmpeg tự động** |
