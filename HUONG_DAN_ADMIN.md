# Hướng dẫn tool Admin — cấp phép theo mã máy, đặt hạn sử dụng

## Cách hoạt động
1. Người dùng mở app lần đầu → app tính **mã máy** (`MRE-XXXX-XXXX-XXXX-XXXX`, theo phần cứng: không đổi kể cả khi cài lại Windows)
   và **tự gửi về máy chủ Admin**. Người dùng có thể ghi thêm tên / SĐT.
2. Trong tool Admin, máy mới hiện ở trạng thái **Chờ duyệt** (có thông báo). Admin bấm **Duyệt…** và nhập số ngày.
3. App của người dùng tự nhận giấy phép (kiểm tra mỗi 20 giây khi đang chờ, mỗi 30 phút khi đang dùng) → tạo mật khẩu → dùng.
4. Đăng nhập app = **mã máy (tự điền) + mật khẩu** người dùng tự đặt. Mật khẩu không bao giờ gửi về Admin.

Giấy phép có **chữ ký số**: app kiểm tra được khi không có mạng và không ai sửa được hạn dùng. Máy Admin **không cần bật 24/7**:
chỉ cần bật khi có người đăng ký mới hoặc khi gia hạn. Hết hạn hoặc bị khoá → app tự khoá, hiện thông báo liên hệ Admin.

## Cài đặt lần đầu (làm 1 lần)
1. Lấy `MovieReviewEditor-Admin.exe` (GitHub Actions → artifact **MovieReviewEditor-Admin**, hoặc chạy `build_admin.bat`).
2. Mở tool Admin → tạo **mật khẩu Admin** (≥ 8 ký tự). Windows hỏi tường lửa → cho phép (để máy khác gọi vào cổng 8800).
3. Ở ô **"Gắn vào bản build"**: sao chép **Khoá công khai** và **Địa chỉ máy chủ**.
4. Gắn vào app khách rồi build lại:
   - GitHub: repo → Settings → Secrets and variables → Actions → tab **Variables** → tạo `LICENSE_PUBKEY` và `LICENSE_SERVER`
     → chạy lại workflow (hoặc đẩy tag mới).
   - Build trên máy: trước khi chạy `build_exe.bat`, mở CMD gõ
     `set MRE_LICENSE_PUBKEY=<khoá>` và `set MRE_LICENSE_SERVER=<địa chỉ>`.
5. **Sao lưu khoá ký**: bấm **Tải file khoá** và cất ở nơi an toàn. Mất file này thì phải build lại app cho mọi máy.
   Chuyển tool Admin sang máy khác: mở tool ở máy mới → **Khôi phục khoá…** → chọn file đã sao lưu.

## Máy khách kết nối tới máy chủ Admin bằng cách nào?
| Trường hợp | Địa chỉ `LICENSE_SERVER` |
|---|---|
| Cùng mạng LAN / văn phòng | `http://<IP-LAN-máy-Admin>:8800` (tool hiện sẵn) |
| Qua Internet, máy Admin ở nhà | Mở cổng 8800 trên router (port forwarding) → `http://<IP-công-cộng>:8800`; nên dùng DDNS nếu IP thay đổi |
| Qua Internet, có tên miền | Cloudflare Tunnel (miễn phí, cố định): `cloudflared tunnel` trỏ tên miền về `http://localhost:8800` → `https://license.tenmien.com` |
| Máy chủ/VPS chạy 24/7 | Chạy `MovieReviewEditor-Admin.exe --server` (Windows VPS) hoặc `pip install -r admin/requirements.txt && python admin/admin_launcher.py --server` (Linux) |

Trang quản lý mặc định **chỉ mở được trên chính máy Admin**. Muốn quản lý từ xa trên VPS: thêm `--allow-remote-admin`
(vẫn cần mật khẩu Admin; nên đặt sau HTTPS). Máy khách chỉ gọi được `/api/v1/register` và `/api/v1/check`.

Máy khách không vào được máy chủ (mạng chặn, máy Admin tắt lâu): bấm **Mã kích hoạt** ở dòng máy đó → gửi chuỗi cho
người dùng (Zalo, email) → họ dán vào **"Có mã kích hoạt từ Admin?"** trên màn hình khoá. Mã chỉ dùng được trên đúng máy đó.

## Việc hằng ngày
- **Duyệt máy mới**: lọc "Chờ duyệt" → **Duyệt…** → nhập số ngày.
- **Gia hạn**: +7 ngày / +30 / +90 / +1 năm (cộng thêm từ ngày hết hạn hiện tại), **Đặt ngày…**, hoặc **Vĩnh viễn**.
- **Khoá máy** (ví dụ ngừng thanh toán): **Khoá** → app trên máy đó tự khoá trong ≤ 30 phút (hoặc lần mở app tới).
- **Người dùng quên mật khẩu**: **Đặt lại MK** → lần mở app tới, họ được tạo mật khẩu mới (dự án, dữ liệu vẫn còn).
- **Dùng thử tự động**: ô "Dùng thử tự động cho máy mới" = số ngày → máy mới tự được dùng ngay N ngày, không cần duyệt.

## Giới hạn cần biết
- Đây là cơ chế cấp phép cho người dùng thông thường. Người có kỹ thuật cao vẫn có thể dịch ngược ứng dụng Python để bỏ
  kiểm tra — không phần mềm cài trên máy người dùng nào chống được hoàn toàn việc này.
- App chặn việc chỉnh lùi đồng hồ máy tính để kéo dài hạn dùng (lệch quá 2 giờ so với lần cuối app thấy / giờ của máy chủ).
- Thay mainboard hoặc CPU → mã máy đổi → máy hiện như máy mới, Admin duyệt lại (có thể xoá dòng máy cũ).
