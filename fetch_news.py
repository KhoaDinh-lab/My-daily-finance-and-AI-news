import os
import json
from google import genai
from google.genai import types

# Initialize Gemini Client
client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

prompt = """
Bạn là một chuyên gia tổng hợp tin tức tài chính và công nghệ.
Hãy tìm kiếm và tổng hợp các tin tức nổi bật trong 24-48 giờ qua về 3 chủ đề:
1. Lạm phát, Lãi suất FED và Kinh tế Vĩ mô Mỹ.
2. Thị trường Việt Nam: Chỉ số VN-Index, Tỷ giá USD/VND, Ngân hàng Nhà nước, Bất động sản, FDI.
3. Cập nhật AI mới nhất: OpenAI, Google Gemini, Anthropic, AI Agents.

Trả về kết quả dưới dạng cấu trúc JSON CHÍNH XÁC như sau (không kèm markdown 
```json):
{
  "updated_at": "Thứ..., Ngày ... Tháng ... Năm ...",
  "tickers": {
    "fed_rate": "3.75%",
    "cpi": "3.5%",
    "vnindex": "1,285.5",
    "usd_vnd": "25,420"
  },
  "macro": [
    { "title": "Tiêu đề tin FED 1", "summary": "Tóm tắt 2-3 câu ngắn gọn", "source": "Tên nguồn tin", "tag": "Thị trường Mỹ" }
  ],
  "vietnam": [
    { "title": "Tiêu đề tin VN-Index/Kinh tế VN 1", "summary": "Tóm tắt 2-3 câu ngắn gọn", "source": "Báo VN", "tag": "Thị trường VN" }
  ],
  "ai": [
    { "title": "Tiêu đề tin AI mới 1", "summary": "Tóm tắt 2-3 câu ngắn gọn", "source": "TechCrunch/OpenAI", "tag": "AI Tech" }
  ]
}
"""

try:
    response = client.models.generate_content(
        model='gemini-2.5-flash',
        contents='Tìm kiếm tin tức tài chính, VN-Index và AI mới nhất hôm nay.',
        config=types.GenerateContentConfig(
            system_instruction=prompt,
            tools=[{"google_search": {}}],
            response_mime_type="application/json"
        )
    )
    
    with open("data.json", "w", encoding="utf-8") as f:
        f.write(response.text)
    print("Đã cập nhật tin tức mới vào data.json thành công!")
except Exception as e:
    print(f"Lỗi khi tải tin tức: {e}")
