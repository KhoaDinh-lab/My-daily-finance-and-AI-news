import os
import json
from google import genai
from google.genai import types

# Khởi tạo API Gemini
api_key = os.environ.get("GEMINI_API_KEY")
client = genai.Client(api_key=api_key)

# Prompt nâng cấp chế độ "DEEP RESEARCH"
deep_research_prompt = """
Bạn là một Chuyên gia Nghiên cứu Chuyên sâu (Senior Deep Researcher) về Tài chính Vĩ mô và Công nghệ AI.

Nhiệm vụ của bạn là thực hiện quy trình NGHIÊN CỨU ĐA NGUỒN (Deep Research) trên Internet trong 24-48 giờ qua:

1. VĨ MÔ & FED:
   - Nghiên cứu từ các nguồn quốc tế uy tín: Bloomberg, Reuters, Financial Times, Wall Street Journal, CNBC.
   - Tập trung: Quyết định lãi suất FED, chỉ số Lạm phát (CPI, PCE), Báo cáo việc làm Mỹ, Dòng tiền toàn cầu.

2. KINH TẾ & THỊ TRƯỜNG VIỆT NAM:
   - Nghiên cứu từ các trang tài chính Việt Nam hàng đầu: CafeF, Vietstock, VnEconomy, Báo Đầu Tư, VTV Money.
   - Tập trung: Diễn biến VN-Index, động thái mua/bán ròng của Khối ngoại, Tỷ giá USD/VND, Lãi suất/Điều hành của Ngân hàng Nhà nước (NHNN), Dòng vốn FDI, Bất động sản & Tín dụng.

3. FRONTIERS OF AI:
   - Nghiên cứu từ: TechCrunch, VentureBeat, MIT Tech Review, ArXiv, OpenAI Blog, Google DeepMind, Anthropic.
   - Tập trung: Các mô hình AI mới ra mắt, Agentic AI, Chip bán dẫn/phần cứng AI, các ứng dụng AI thực tế vào doanh nghiệp.

YÊU CẦU NGHIÊN CỨU SÂU:
- Lọc bỏ tất cả tin rác, tin giật gân clickbait.
- Chỉ chọn 2-3 tin CÓ GIÁ TRỊ NHẤT cho mỗi lĩnh vực.
- Tóm tắt súc tích, đi thẳng vào bản chất vấn đề và tác động đến dòng tiền/nền kinh tế.

Trả về kết quả chuẩn định dạng JSON như sau (không thêm bất kỳ đoạn văn bản nào khác ngoài JSON):
{
  "updated_at": "Hôm nay",
  "tickers": {
    "fed_rate": "3.75%",
    "cpi": "3.5%",
    "vnindex": "1,285.5",
    "usd_vnd": "25,420"
  },
  "macro": [
    { "title": "Tiêu đề phân tích sâu 1", "summary": "Tóm tắt phân tích 2-3 câu có số liệu", "source": "Tên nguồn uy tín", "tag": "Lạm Phát / FED" }
  ],
  "vietnam": [
    { "title": "Tiêu đề phân tích sâu 1", "summary": "Tóm tắt phân tích 2-3 câu có số liệu", "source": "Tên nguồn VN", "tag": "VN-Index / Dòng Tiền" }
  ],
  "ai": [
    { "title": "Tiêu đề đột phá AI 1", "summary": "Tóm tắt đột phá 2-3 câu", "source": "Tên nguồn Tech", "tag": "AI Tech / Agents" }
  ]
}
"""

try:
    response = client.models.generate_content(
        model='gemini-2.0-flash',
        contents='Hãy thực hiện Deep Research tìm kiếm và tổng hợp các thông tin tài chính vĩ mô, VN-Index và AI quan trọng nhất hôm nay.',
        config=types.GenerateContentConfig(
            system_instruction=deep_research_prompt,
            tools=[{"google_search": {}}], # Kích hoạt Google Search Grounding để Gemini lướt web
            response_mime_type="application/json"
        )
    )
    
    # Lưu dữ liệu vào data.json
    with open("data.json", "w", encoding="utf-8") as f:
        f.write(response.text)
    print("Đã thực hiện Deep Research và cập nhật dữ liệu thành công!")
except Exception as e:
    print(f"Lỗi khi Deep Research: {e}")
    raise e
