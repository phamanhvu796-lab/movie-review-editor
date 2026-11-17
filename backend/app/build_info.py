# File này được GitHub Actions / build_exe.bat GHI ĐÈ lúc build. Giá trị dưới đây là mặc định khi chạy mã nguồn.
UPDATE_REPO = ""      # "chu-repo/ten-repo" trên GitHub: nơi app tìm bản mới (mục Releases)
DIST = "source"       # setup (cài bằng Setup.exe) | portable (1 file exe) | folder (thư mục giải nén) | source
LICENSE_SERVER = ""   # địa chỉ máy chủ của tool Admin, vd: https://admin.tenmien.com hoặc http://192.168.1.10:8800
LICENSE_PUBKEY = ""   # khoá công khai do tool Admin tạo. Để trống = không yêu cầu giấy phép (dùng tự do)
