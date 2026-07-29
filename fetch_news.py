import os
import json
from google import genai
from google.genai import types

# Khởi tạo kết nối với Gemini
api_key = os.environ.get("GEMINI_API_KEY")
client = genai.Client(api_key=api_key)

prompt = """
Bạn là một chuyên gia tổng hợp tin tức tài chính và công nghệ.
Hãy tìm kiếm trên Google và tổng hợp các tin tức mới nhất hôm nay về 3 chủ đề:
1. Lạm phát, Lãi suất FED và Kinh tế Vĩ mô Mỹ.
2. Thị trường Việt Nam: Chỉ số VN-Index, Tỷ giá USD/VND, Ngân hàng Nhà nước, FDI.
3. Cập nhật AI mới nhất: OpenAI, Google Gemini, Anthropic, AI Agents.

Hãy trả về định dạng JSON chuẩn:
{
  "updated_at": "Hôm nay",
  "tickers": {
    "fed_rate": "3.75%",
    "cpi": "3.5%",
    "vnindex": "1,285.5",
    "usd_vnd": "25,420"
  },
  "macro": [
    { "title": "Tiêu đề tin FED", "summary": "Tóm tắt tin 2-3 câu", "source": "Báo quốc tế", "tag": "Thị trường Mỹ" }
  ],
  "vietnam": [
    { "title": "Tiêu đề tin VN-Index", "summary": "Tóm tắt tin 2-3 câu", "source": "Báo VN", "tag": "Thị trường VN" }
  ],
  "ai": [
    { "title": "Tiêu đề tin AI", "summary": "Tóm tắt tin 2-3 câu", "source": "Tech News", "tag": "AI Tech" }
  ]
}
"""

try:
    response = client.models.generate_content(
        model='gemini-2.0-flash',
        contents='Tìm kiếm tin tức tài chính, VN-Index và AI mới nhất hôm nay.',
        config=types.GenerateContentConfig(
            system_instruction=prompt,
            tools=[{"google_search": {}}],
            response_mime_type="application/json"
        )
    )
    
    # Lưu kết quả vào file data.json
    with open("data.json", "w", encoding="utf-8") as f:
        f.write(response.text)
    print("Đã cập nhật tin tức mới vào data.json thành công!")
except Exception as e:
    print(f"Lỗi khi tải tin tức: {e}")
    raise e
