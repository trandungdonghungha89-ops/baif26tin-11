# HƯỚNG DẪN ĐƯA TRÒ CHƠI LÊN INTERNET BẰNG RENDER

## 1. Chuẩn bị
- Tạo tài khoản GitHub: https://github.com
- Tạo tài khoản Render: https://render.com

## 2. Đưa mã nguồn lên GitHub
1. Giải nén gói trò chơi.
2. Tạo repository mới trên GitHub, ví dụ: `insertion-sort-class-game`.
3. Upload TOÀN BỘ các tệp và thư mục bên trong thư mục trò chơi lên thư mục gốc repository.
4. Kiểm tra ở thư mục gốc phải có: `app.py`, `requirements.txt`, `render.yaml`, `Procfile`, `templates/`.
5. Trong `templates/` phải có `index.html`.

## 3. Tạo dịch vụ bằng Render Blueprint
1. Đăng nhập Render.
2. Chọn New -> Blueprint.
3. Kết nối GitHub nếu được yêu cầu.
4. Chọn repository `insertion-sort-class-game`.
5. Render tự đọc `render.yaml`.
6. Xác nhận Deploy Blueprint.

## 4. Mở trò chơi
Khi deploy thành công, Render cấp địa chỉ dạng:
`https://ten-dich-vu.onrender.com`

Mở địa chỉ này trên máy giáo viên.
- Chọn chế độ giáo viên.
- Tạo phòng.
- Mã phòng và QR xuất hiện.
- Học sinh quét QR bằng Wi-Fi/4G/5G bất kỳ.

## 5. Trước giờ dạy
Với gói Free, Render có thể đưa dịch vụ về trạng thái ngủ sau một thời gian không hoạt động. Hãy mở đường link trò chơi trước khi bắt đầu hoạt động trên lớp để dịch vụ sẵn sàng.

## 6. Lưu ý dữ liệu
Phiên bản hiện tại dùng SQLite trong filesystem của web service. Dữ liệu phòng chơi/kết quả phù hợp cho từng buổi chơi trực tiếp nhưng có thể mất khi Render redeploy hoặc restart dịch vụ. Mã nguồn trò chơi trên GitHub không bị mất.
