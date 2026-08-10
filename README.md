# The Daily Edge

Website tin tức cá nhân cập nhật tự động về tài chính, AI, logistics và bất động sản.

## Tài khoản và đồng bộ

Website dùng Supabase Auth + Postgres để đồng bộ streak và bài đã đọc giữa nhiều thiết bị.

- Supabase project: `jayiozatpkaltpuhueuh`
- Trang web chỉ chứa publishable key dành cho trình duyệt; không chứa secret key hoặc service-role key.
- Row Level Security bảo đảm mỗi tài khoản chỉ đọc và sửa dữ liệu của chính mình.
- Bản migration có thể kiểm tra tại `supabase/migrations/20260810054100_create_user_reading_sync.sql`.

Người dùng mới chọn **Tạo tài khoản**, nhập tên hiển thị, email và mật khẩu. Nếu Supabase yêu cầu xác nhận email, hãy mở liên kết trong email trước khi đăng nhập.
