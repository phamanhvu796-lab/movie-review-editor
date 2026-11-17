> **Cài nhanh:** chỉ cần 1 file `MovieReviewEditor-Setup-X.Y.Z.exe` → bấm đôi → app tự cài và mở. Hoặc dùng
> `MovieReviewEditor-Portable-X.Y.Z.exe` (chạy ngay, không cài). Đừng chép riêng file exe trong thư mục build.
> Có bản mới: bấm nút tím **⬆ Cập nhật** trên thanh trên (hoặc ⚙ Cài đặt → Cập nhật phiên bản).

# Cài đặt Movie Review Editor (dành cho người nhận)

**Yêu cầu:** Windows 10/11 64-bit, ~3 GB ổ trống, có Internet (để gọi AI). Không cần cài Python hay FFmpeg.

## Cài
1. Bấm đôi `MovieReviewEditor-Setup-x.y.z.exe`.
2. Nếu Windows hiện **"Windows protected your PC"** (SmartScreen): bấm **More info** → **Run anyway**. *(Hiện cảnh báo vì file chưa có chữ ký số của nhà phát hành, không phải do có virus.)*
3. Bấm **Next** → **Install**. Không cần quyền Admin. Có thể tick tạo shortcut ở Desktop.
4. Bấm **Finish** để mở app.

## Dùng lần đầu
- App mở trong **cửa sổ riêng** như mọi phần mềm (không cần trình duyệt). Đóng cửa sổ = thoát app.
- **Màn hình khoá:** lần đầu hãy **tạo key bảo mật + mật khẩu** (ghi nhớ lại); các lần sau nhập đúng cả hai để vào.
  (Nếu người gửi app đã cấp sẵn key + mật khẩu thì nhập đúng thông tin được cấp.)
- Vào giao diện tool → ⚙ **Cài đặt**: **dán API key** (và Base URL nếu dùng relay) — app **tự kết nối**, không cần bấm nút. Key chỉ lưu trên máy bạn.
- Lần đầu dùng Whisper (tạo phụ đề từ giọng) sẽ tải model ~500 MB từ Internet.

## Dữ liệu nằm ở đâu
`C:\Users\<tên bạn>\MovieReviewEditor` (dự án, video đã xuất, cài đặt, nhật ký `logs\app.log`). Cài bản mới **đè lên bản cũ** không mất dữ liệu.

## Gỡ cài đặt
Settings → Apps → Movie Review Editor → Uninstall. Sẽ có hỏi có xoá luôn dữ liệu hay giữ lại.

## Gặp sự cố
| Hiện tượng | Cách xử lý |
|---|---|
| Báo mất kết nối máy chủ nội bộ | Đóng cửa sổ app rồi mở lại từ Start Menu |
| Quên key bảo mật / mật khẩu | Đóng app, xoá file `auth.json` trong thư mục dữ liệu bên dưới, mở lại để tạo mới (dự án vẫn còn) |
| Antivirus xoá/chặn file | Thêm thư mục cài đặt vào danh sách cho phép (file exe dạng đóng gói Python hay bị báo nhầm) |
| Giọng đọc báo thiếu FFmpeg | Mục **Giọng AI** → **Tải FFmpeg tự động** (thường đã đi kèm sẵn) |
| Cửa sổ trắng / báo thiếu WebView2 | Cài "Microsoft Edge WebView2 Runtime" (bộ cài Setup.exe đã tự làm việc này) |
