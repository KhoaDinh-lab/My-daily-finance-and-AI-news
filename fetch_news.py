import json
import os
import re
import sys
import traceback
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from google import genai
from google.genai import types


OUTPUT_FILE = Path(__file__).with_name("data.json")
REQUIRED_SECTIONS = ("macro", "vietnam", "ai")
REQUIRED_TICKERS = ("fed_rate", "cpi", "vnindex", "usd_vnd")
REQUIRED_NEWS_FIELDS = ("title", "summary", "source", "tag")

# Hai model stable này hỗ trợ Google Search trên Gemini API Free Tier.
# Có thể đặt GEMINI_MODEL trong GitHub Secrets/Variables để ép dùng một model khác.
DEFAULT_MODELS = ("gemini-2.5-flash", "gemini-2.5-flash-lite")

SYSTEM_PROMPT = """
Bạn là chuyên gia nghiên cứu tin tức về tài chính vĩ mô, thị trường Việt Nam
và công nghệ AI. Hãy dùng Google Search để kiểm chứng thông tin, ưu tiên nguồn
gốc hoặc nguồn báo chí uy tín. Không được bịa số liệu, tiêu đề hoặc nguồn tin.

Thu thập thông tin mới trong 24-48 giờ gần nhất cho ba nhóm:
1. Vĩ mô & FED: CPI, lãi suất FED và kinh tế Mỹ.
2. Việt Nam: VN-Index, USD/VND, NHNN, FDI và diễn biến thị trường.
3. AI: model mới, agentic AI, chip AI và thông báo từ các hãng AI.

Trả về DUY NHẤT một JSON object hợp lệ, không dùng markdown và không thêm lời
giải thích. Mỗi nhóm tin nên có 3 mục nếu có đủ tin đáng tin cậy. Dùng cấu trúc:
{
  "updated_at": "ngày giờ cập nhật theo giờ Việt Nam",
  "tickers": {
    "fed_rate": "giá trị mới nhất",
    "cpi": "giá trị mới nhất",
    "vnindex": "giá trị mới nhất",
    "usd_vnd": "giá trị mới nhất"
  },
  "macro": [
    {"title": "...", "summary": "2-3 câu có số liệu", "source": "...", "tag": "..."}
  ],
  "vietnam": [
    {"title": "...", "summary": "2-3 câu có số liệu", "source": "...", "tag": "..."}
  ],
  "ai": [
    {"title": "...", "summary": "2-3 câu", "source": "...", "tag": "..."}
  ]
}
"""


def models_to_try() -> tuple[str, ...]:
    configured_model = os.environ.get("GEMINI_MODEL", "").strip()
    return (configured_model,) if configured_model else DEFAULT_MODELS


def extract_json(raw_text: str) -> dict:
    """Parse JSON even if the model accidentally wraps it in a Markdown fence."""
    text = raw_text.strip().lstrip("\ufeff")
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)

    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        # Last-resort recovery for a short sentence before/after the JSON object.
        object_start = text.find("{")
        if object_start == -1:
            raise
        result, _ = json.JSONDecoder().raw_decode(text[object_start:])

    if not isinstance(result, dict):
        raise ValueError("Gemini không trả về một JSON object.")
    return result


def validate_news_data(data: dict) -> None:
    """Reject incomplete output so a bad response never replaces data.json."""
    if not isinstance(data.get("updated_at"), str) or not data["updated_at"].strip():
        raise ValueError("Thiếu trường updated_at hợp lệ.")

    tickers = data.get("tickers")
    if not isinstance(tickers, dict):
        raise ValueError("Thiếu object tickers.")
    for key in REQUIRED_TICKERS:
        if not isinstance(tickers.get(key), str) or not tickers[key].strip():
            raise ValueError(f"Ticker '{key}' bị thiếu hoặc không hợp lệ.")

    for section in REQUIRED_SECTIONS:
        articles = data.get(section)
        if not isinstance(articles, list) or not articles:
            raise ValueError(f"Mục '{section}' không có bài viết.")
        for index, article in enumerate(articles, start=1):
            if not isinstance(article, dict):
                raise ValueError(f"{section}[{index}] không phải object.")
            for field in REQUIRED_NEWS_FIELDS:
                value = article.get(field)
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(
                        f"{section}[{index}] thiếu trường '{field}' hợp lệ."
                    )


def fetch_news(client: genai.Client) -> tuple[dict, str]:
    today = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).strftime("%d/%m/%Y %H:%M")
    request = (
        f"Thời điểm hiện tại tại Việt Nam là {today}. "
        "Hãy tìm kiếm, kiểm chứng và tổng hợp bản tin mới nhất theo đúng cấu trúc JSON."
    )
    errors: list[str] = []

    for model_name in models_to_try():
        print(f"Đang gọi Gemini model: {model_name}...", flush=True)
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=request,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                    temperature=0.2,
                ),
            )
            response_text = response.text if response else None
            if not response_text:
                raise ValueError("API trả về response rỗng hoặc bị chặn.")

            data = extract_json(response_text)
            validate_news_data(data)
            print(f"Gemini model {model_name} trả về dữ liệu hợp lệ.", flush=True)
            return data, model_name
        except Exception as error:  # Continue to the free-tier fallback model.
            detail = f"{type(error).__name__}: {error}"
            errors.append(f"{model_name}: {detail}")
            print(f"Model {model_name} thất bại: {detail}", file=sys.stderr, flush=True)

    raise RuntimeError("Tất cả model đều thất bại:\n- " + "\n- ".join(errors))


def write_json_atomically(data: dict) -> None:
    temporary_file = OUTPUT_FILE.with_suffix(".json.tmp")
    with temporary_file.open("w", encoding="utf-8", newline="\n") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write("\n")
    temporary_file.replace(OUTPUT_FILE)


def main() -> int:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        print(
            "LỖI: Không tìm thấy GEMINI_API_KEY. Hãy thêm key vào "
            "GitHub Settings > Secrets and variables > Actions.",
            file=sys.stderr,
        )
        return 1

    try:
        client = genai.Client(api_key=api_key)
        data, model_name = fetch_news(client)
        write_json_atomically(data)
        print(f"Đã cập nhật {OUTPUT_FILE.name} bằng {model_name}.")
        return 0
    except Exception:
        print("LỖI KHI CẬP NHẬT TIN TỨC:", file=sys.stderr)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
