# The Daily Edge

Website tin tức cá nhân cập nhật tự động về tài chính, AI, logistics và bất động sản.

Tài liệu đầy đủ về kiến trúc, nguồn dữ liệu, cách cài đặt và vận hành: [TAI_LIEU_WEBSITE.md](TAI_LIEU_WEBSITE.md).

## Tài khoản và đồng bộ

Website dùng Supabase Auth + Postgres để đồng bộ streak và bài đã đọc giữa nhiều thiết bị.

- Supabase project: `jayiozatpkaltpuhueuh`
- Trang web chỉ chứa publishable key dành cho trình duyệt; không chứa secret key hoặc service-role key.
- Row Level Security bảo đảm mỗi tài khoản chỉ đọc và sửa dữ liệu của chính mình.
- Bản migration có thể kiểm tra tại `supabase/migrations/20260810054100_create_user_reading_sync.sql`.

## Founder Beta

Trang `#pro` dùng để kiểm chứng nhu cầu trước khi mở thanh toán. Người dùng có thể chọn gói, mức giá và tính năng mong muốn; website **không thu tiền** ở giai đoạn này. Dữ liệu được lưu trong `monetization_leads`, còn funnel tối giản được ghi vào `product_events`. Cả hai bảng đều bật Row Level Security.

Người dùng mới chọn **Tạo tài khoản**, nhập tên hiển thị, email và mật khẩu. Nếu Supabase yêu cầu xác nhận email, hãy mở liên kết trong email trước khi đăng nhập.
