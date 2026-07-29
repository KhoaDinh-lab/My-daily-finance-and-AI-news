import os
import json
import re
import traceback
from google import genai
from google.genai import types

# 1. Lấy chìa khóa API từ Secret
api_key = os.environ.get("GEMINI_API_KEY")
if not api_key:
    print("❌ LỖI RẤT QUAN TRỌNG: Bạn chưa dán GEMINI_API_KEY vào Secrets của GitHub!")
    exit(1)

client = genai.Client(api_key=api_key)

# 2. Prompt Deep Research
deep_research_prompt = """
Bạn là một Chuyên gia Nghiên cứu Chuyên sâu (Senior Deep Researcher) về Tài chính Vĩ mô và Công nghệ AI.

Nhiệm vụ của bạn là thực hiện NGHIÊN CỨU ĐA NGUỒN (Deep Research) trên Internet trong 24-48 giờ qua:

1. VĨ MÔ & FED: Bloomberg, Reuters, Financial Times, Wall Street Journal. (Lạm phát CPI, Lãi suất FED, Kinh tế Mỹ).
2. THỊ TRƯỜNG VIỆT NAM: CafeF, Vietstock, VnEconomy, Báo Đầu Tư. (VN-Index, Tỷ giá USD/VND, NHNN, FDI).
3. FRONTIERS OF AI: TechCrunch, VentureBeat, OpenAI Blog, Google DeepMind, Anthropic. (Mô hình AI mới, Agentic AI, Chip AI).

YÊU CẦU TRẢ VỀ DUY NHẤT ĐỊNH DẠNG JSON (KHÔNG KÈM BẤT KỲ VĂN BẢN NÀO BÊN NGOÀI):
{
  "updated_at": "Hôm nay",
  "tickers": {
    "fed_rate": "3.75%",
    "cpi": "3.5%",
    "vnindex": "1,285.5",
    "usd_vnd": "25,420"
  },
  "macro": [
    { "title": "Tiêu đề tin FED", "summary": "Tóm tắt 2-3 câu có số liệu", "source": "Nguồn tin", "tag": "Lạm Phát / FED" }
  ],
  "vietnam": [
    { "title": "Tiêu đề tin VN-Index", "summary": "Tóm tắt 2-3 câu có số liệu", "source": "Nguồn VN", "tag": "VN-Index" }
  ],
  "ai": [
    { "title": "Tiêu đề tin AI mới", "summary": "Tóm tắt đột phá 2-3 câu", "source": "Nguồn Tech", "tag": "AI Tech" }
  ]
}
"""

# Danh sách các tên model để thử lần lượt (chống lỗi tên model)
models_to_try = [
    'gemini-3.6-flash',
    'gemini-3.5-flash-lite',
    'gemini-3.1-pro',
    'gemini-2.5-flash',
    'gemini-2.0-flash'
]
response = None
for model_name in models_to_try:
    try:
        print(f"🔍 Đang thử kết nối Gemini với model: {model_name}...")
        response = client.models.generate_content(
            model=model_name,
            contents='Hãy thực hiện Deep Research tìm kiếm thông tin tài chính vĩ mô, VN-Index và AI mới nhất hôm nay.',
            config=types.GenerateContentConfig(
                system_instruction=deep_research_prompt,
                tools=[{"google_search": {}}],
                response_mime_type="application/json"
            )
        )
        if response and response.text:
            print(f"✅ Kết nối thành công với {model_name}!")
            break
    except Exception as err:
        print(f"⚠️ Model {model_name} chưa phản hồi, thử model tiếp theo... (Lỗi: {err})")

if not response or not response.text:
    print("❌ LỖI: Tất cả các model Gemini đều không thể lấy dữ liệu!")
    exit(1)

try:
    raw_text = response.text.strip()
    
    # Làm sạch chuỗi JSON nếu có dính thẻ markdown
    clean_json = re.sub(r'^
```json\s*', '', raw_text, flags=re.MULTILINE)
    clean_json = re.sub(r'^
```\s*', '', clean_json, flags=re.MULTILINE)
    clean_json = clean_json.strip()

    # Kiểm tra JSON hợp lệ
    json_data = json.loads(clean_json)

    # Ghi ra tệp data.json
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(json_data, f, ensure_ascii=False, indent=2)
        
    print("🎉 TẬP TIN DATA.JSON ĐÃ ĐƯỢC TẠO THÀNH CÔNG!")

except Exception as e:
    print("❌ LỖI XỬ LÝ DỮ LIỆU JSON:")
    print(traceback.format_exc())
    exit(1)
