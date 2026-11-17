# Hướng dẫn tool Admin — cấp phép theo mã máy, đặt hạn sử dụng

## Cách hoạt động (từ v1.5.0: theo TÀI KHOẢN)
1. Người dùng mở app → tab **Đăng ký**: tên đăng nhập, mật khẩu, họ tên, SĐT/Zalo → yêu cầu gửi về tool Admin.
2. Tool Admin hiện ở mục **🔔 Yêu cầu chờ xử lý** → bấm **✓ Duyệt & tạo mã…** → nhập số ngày → hiện **MÃ KÍCH HOẠT**
   (dạng `K7QM-3XPA`, dùng 1 lần, hết hạn sau 7 ngày) kèm tin nhắn mẫu → **Sao chép tin nhắn** → gửi qua Zalo/SMS.
3. Người dùng nhập mã vào app → **Kích hoạt máy này** → **Đăng nhập**.
4. Từ đó trên máy này: chỉ cần **tên đăng nhập + mật khẩu**, **không cần mạng**.
5. **Đăng nhập tài khoản sang máy khác** → app tự gửi yêu cầu "Đăng nhập trên máy mới" → Admin bấm **🔑 Tạo mã kích hoạt** →
   gửi mã → máy mới dùng được. Mặc định mỗi tài khoản dùng **1 máy**: kích hoạt máy mới thì máy cũ tự bị thu hồi
   (đổi được bằng nút **Số máy**).
6. **Cài lại Windows trên cùng máy**: mã máy theo phần cứng không đổi → chỉ cần đăng nhập lại là dùng tiếp, không cần mã.

Mật khẩu được app băm trước khi gửi đi; Admin không xem được mật khẩu của người dùng. Hạn dùng/khoá đặt theo tài khoản.
Giấy phép có **chữ ký số**: app kiểm tra được khi không có mạng và không ai sửa được hạn dùng. Máy Admin **không cần bật 24/7**:
chỉ cần bật khi có người đăng ký / đổi máy / gia hạn.

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
- **Duyệt đăng ký / máy mới**: mục 🔔 Yêu cầu chờ xử lý → **Duyệt & tạo mã** (tài khoản mới: nhập số ngày) → gửi mã. Không duyệt: **Từ chối**.
- **Gia hạn**: +7 ngày / +30 / +90 / +1 năm (cộng thêm từ ngày hết hạn hiện tại), **Đặt ngày…**, hoặc **Vĩnh viễn**.
- **Khoá tài khoản** (ví dụ ngừng thanh toán): **Khoá** → app tự khoá trong ≤ 30 phút hoặc ở lần đăng nhập tới.
- **Người dùng quên mật khẩu**: **Đặt mật khẩu mới** → gửi mật khẩu đó cho họ → họ đăng nhập (cần mạng) rồi tự đổi trong ⚙ Cài đặt.
- **Thu hồi một máy**: nút **Thu hồi** ở dòng máy đó.
- **Tạo sẵn tài khoản cho khách**: **+ Tạo tài khoản** → khách chỉ cần Đăng nhập trong app → bạn tạo mã cho máy của họ.
- **Máy khách không vào được máy chủ**: nút **Mã offline** ở dòng máy (chuỗi dài) → khách dán vào ô Mã kích hoạt.
- **Dùng thử**: ô "Dùng thử khi đăng ký" = số ngày → khách đăng ký là dùng được ngay N ngày, không cần mã.

## Giới hạn cần biết
- Đây là cơ chế cấp phép cho người dùng thông thường. Người có kỹ thuật cao vẫn có thể dịch ngược ứng dụng Python để bỏ
  kiểm tra — không phần mềm cài trên máy người dùng nào chống được hoàn toàn việc này.
- App chặn việc chỉnh lùi đồng hồ máy tính để kéo dài hạn dùng (lệch quá 2 giờ so với lần cuối app thấy / giờ của máy chủ).
- Thay mainboard hoặc CPU → mã máy đổi → máy hiện như máy mới, Admin duyệt lại (có thể xoá dòng máy cũ).
