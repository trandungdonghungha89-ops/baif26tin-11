# Kho trò chơi Tin học

Website Flask dùng chung một Render Web Service cho nhiều trò chơi.

## Trò chơi hiện có
1. `/insertion-sort` – Ghép chương trình Insertion Sort, phòng QR 120 giây, bảng kết quả, học sinh lên bảng chơi.
2. `/millionaire` – Ai Là Triệu Phú Bài 26, 10 câu hỏi, 3 quyền trợ giúp, QR khán giả 15 giây.

## Trang chủ
`/` – Kho trò chơi Tin học.

## Triển khai
Render dùng:
- Build: `pip install -r requirements.txt`
- Start: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 8`

Giữ `--workers 1` vì phần bình chọn khán giả của game Triệu Phú dùng bộ nhớ tiến trình trong một phiên lớp học.


## Bổ sung mới
- Thêm game **Escape Room – Phòng thoát hiểm Tin học (Bài 29 Tin học 10)** vào cùng website.
- Đường dẫn: `/escape-bai29` (giáo viên) và `/escape-bai29/join` (học sinh).
