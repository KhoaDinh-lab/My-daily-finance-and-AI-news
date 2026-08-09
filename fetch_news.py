import json
import os
import re
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from groq import Groq


OUTPUT_FILE = Path(__file__).with_name("data.json")

REQUIRED_SECTIONS = ("macro", "vietnam", "ai")
REQUIRED_TICKERS = ("fed_rate", "cpi", "vnindex", "usd_vnd")
REQUIRED_NEWS_FIELDS = ("title", "summary", "source", "tag")

# Groq Compound có khả năng tìm kiếm thông tin mới trên web.
MODELS_TO_TRY = (
    "groq/compound",
    "groq/compound-mini",
)


SYSTEM_PROMPT = """
Bạn là biên tập viên của website tin tức The Daily Edge.

Nhiệm vụ:
- Tìm kiếm và kiểm chứng tin tức mới trong 24-48 giờ gần nhất.
- Ưu tiên nguồn chính thức hoặc báo chí uy tín.
- Không được tự bịa tiêu đề, số liệu, nguồn tin hoặc sự kiện.
- Nếu chưa tìm được một số liệu đáng tin cậy, ghi "Chưa có dữ liệu".
- Viết bằng tiếng Việt dễ hiểu.

Các nhóm cần thu thập:

1. macro:
   FED, lãi suất Mỹ, CPI, lạm phát và kinh tế Mỹ.
   Ưu tiên federalreserve.gov, bls.gov, Reuters, Bloomberg, Financial Times.

2. vietnam:
   VN-Index, USD/VND, NHNN, FDI và thị trường chứng khoán Việt Nam.
   Ưu tiên nguồn chính thức, CafeF, Vietstock, VnEconomy, Báo Đầu Tư.

3. ai:
   Mô hình AI mới, AI Agent, chip AI và thông báo từ các công ty AI.
   Ưu tiên blog chính thức của công ty, Reuters, TechCrunch và VentureBeat.

Chỉ trả về một JSON object hợp lệ, không dùng Markdown và không thêm
lời giải thích bên ngoài JSON.

Cấu trúc bắt buộc:

{
  "updated_at": "thời gian cập nhật",
  "tickers": {
    "fed_rate": "mức lãi suất mục tiêu mới nhất của FED",
    "cpi": "giá trị CPI Mỹ mới nhất",
    "vnindex": "điểm VN-Index mới nhất",
    "usd_vnd": "tỷ giá USD/VND mới nhất"
  },
  "macro": [
    {
      "title": "tiêu đề",
      "summary": "tóm tắt 2-3 câu",
      "source": "tên nguồn",
      "tag": "FED hoặc CPI hoặc Kinh tế Mỹ"
    }
  ],
  "vietnam": [
    {
      "title": "tiêu đề",
      "summary": "tóm tắt 2-3 câu",
      "source": "tên nguồn",
      "tag": "VN-Index hoặc NHNN hoặc FDI"
    }
  ],
  "ai": [
    {
      "title": "tiêu đề",
      "summary": "tóm tắt 2-3 câu",
      "source": "tên nguồn",
      "tag": "AI Tech"
    }
  ]
}

Mỗi nhóm nên có 3 tin nếu tìm được đủ nguồn đáng tin cậy.
"""


def download_json(url):
    """Tải JSON từ một API công khai."""
    request = Request(
        url,
        headers={
            "User-Agent": "TheDailyEdge/1.0",
            "Accept": "application/json",
        },
    )

    last_error = None
    for attempt in range(1, 4):
        try:
            with urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as error:
            last_error = error
            if attempt == 3:
                break

            wait_seconds = attempt * 3
            print(
                f"API dữ liệu tạm lỗi; chờ {wait_seconds} giây rồi thử lại...",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(wait_seconds)

    raise last_error


def load_previous_tickers():
    """Đọc số liệu cũ để dự phòng khi API dữ liệu tạm thời bị lỗi."""
    if not OUTPUT_FILE.exists():
        return {}

    try:
        with OUTPUT_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)

        tickers = data.get("tickers", {})
        return tickers if isinstance(tickers, dict) else {}
    except Exception:
        return {}


def fetch_us_cpi():
    """
    Lấy CPI Mỹ từ BLS.

    CUUR0000SA0 là CPI-U, All items, U.S. city average,
    không điều chỉnh theo mùa.
    """
    url = (
        "https://api.bls.gov/publicAPI/v1/timeseries/data/"
        "CUUR0000SA0"
    )
    payload = download_json(url)

    status = payload.get("status")
    if status != "REQUEST_SUCCEEDED":
        raise RuntimeError(f"BLS API không thành công: {status}")

    series = payload["Results"]["series"][0]["data"]

    monthly_values = {}
    for item in series:
        period = item.get("period", "")
        year = item.get("year", "")
        value = item.get("value", "")

        # Chỉ lấy các tháng M01 đến M12; bỏ M13 là trung bình năm.
        if not re.fullmatch(r"M(0[1-9]|1[0-2])", period):
            continue

        monthly_values[(int(year), int(period[1:]))] = float(value)

    if not monthly_values:
        raise RuntimeError("BLS không trả về dữ liệu CPI theo tháng.")

    latest_year, latest_month = max(monthly_values)
    latest_value = monthly_values[(latest_year, latest_month)]
    previous_value = monthly_values.get((latest_year - 1, latest_month))

    if previous_value is None:
        raise RuntimeError("Không đủ dữ liệu để tính CPI cùng kỳ năm trước.")

    inflation = (latest_value / previous_value - 1) * 100
    return f"{inflation:.1f}%"


def fetch_usd_vnd():
    """Lấy tỷ giá tham khảo USD/VND từ ExchangeRate-API."""
    payload = download_json(
        "https://open.er-api.com/v6/latest/USD"
    )

    if payload.get("result") != "success":
        raise RuntimeError("ExchangeRate-API không trả về thành công.")

    vnd_rate = payload.get("rates", {}).get("VND")
    if not isinstance(vnd_rate, (int, float)):
        raise RuntimeError("Không tìm thấy VND trong dữ liệu tỷ giá.")

    return f"{vnd_rate:,.0f}"


def get_value_or_fallback(fetch_function, ticker_name, old_tickers):
    """Nếu API tạm lỗi thì giữ số liệu cũ thay vì làm hỏng cả website."""
    try:
        value = fetch_function()
        print(f"Đã lấy {ticker_name}: {value}", flush=True)
        return value
    except Exception as error:
        fallback = old_tickers.get(ticker_name, "Chưa có dữ liệu")
        print(
            f"CẢNH BÁO: Không lấy được {ticker_name}: {error}. "
            f"Sử dụng giá trị dự phòng: {fallback}",
            file=sys.stderr,
            flush=True,
        )
        return fallback


def extract_json(raw_text):
    """Đọc JSON kể cả khi AI vô tình thêm dấu Markdown."""
    text = raw_text.strip().lstrip("\ufeff")
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)

    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start == -1:
            raise

        result, _ = json.JSONDecoder().raw_decode(text[start:])

    if not isinstance(result, dict):
        raise ValueError("Groq không trả về một JSON object.")

    return result


def validate_news_data(data):
    """Ngăn phản hồi thiếu dữ liệu ghi đè lên data.json đang hoạt động."""
    tickers = data.get("tickers")
    if not isinstance(tickers, dict):
        raise ValueError("Thiếu mục tickers.")

    for ticker in REQUIRED_TICKERS:
        value = tickers.get(ticker)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Ticker {ticker} không hợp lệ.")

    for section in REQUIRED_SECTIONS:
        articles = data.get(section)

        if not isinstance(articles, list) or not articles:
            raise ValueError(f"Mục {section} không có bài viết.")

        for index, article in enumerate(articles, start=1):
            if not isinstance(article, dict):
                raise ValueError(f"{section}[{index}] không phải object.")

            for field in REQUIRED_NEWS_FIELDS:
                value = article.get(field)
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(
                        f"{section}[{index}] thiếu trường {field}."
                    )


def fetch_news_from_groq(client, cpi, usd_vnd):
    vietnam_time = datetime.now(
        ZoneInfo("Asia/Ho_Chi_Minh")
    ).strftime("%d/%m/%Y %H:%M")

    user_prompt = f"""
Thời gian hiện tại tại Việt Nam: {vietnam_time}.

Hãy dùng công cụ tìm kiếm web để tạo bản tin mới nhất.

Hai số liệu đã được lấy trực tiếp từ API dữ liệu:
- CPI Mỹ theo năm: {cpi}
- Tỷ giá tham khảo USD/VND: {usd_vnd}

Không tự thay đổi hai số liệu trên.

Đối với fed_rate và vnindex:
- Chỉ điền số liệu nếu tìm được nguồn mới và đáng tin cậy.
- Nếu không chắc chắn, ghi "Chưa có dữ liệu".

Trả về đúng JSON theo cấu trúc được yêu cầu.
"""

    errors = []

    for model_name in MODELS_TO_TRY:
        for attempt in range(1, 4):
            print(
                f"Đang gọi Groq model: {model_name} (lần {attempt}/3)...",
                flush=True,
            )

            try:
                response = client.chat.completions.create(
                    model=model_name,
                    messages=[
                        {
                            "role": "system",
                            "content": SYSTEM_PROMPT,
                        },
                        {
                            "role": "user",
                            "content": user_prompt,
                        },
                    ],
                    temperature=0.1,
                    max_completion_tokens=2500,
                )

                content = response.choices[0].message.content
                if not content:
                    raise ValueError("Groq trả về nội dung rỗng.")

                data = extract_json(content)

                # Luôn dùng dữ liệu trực tiếp từ API cho hai chỉ số này.
                data.setdefault("tickers", {})
                data["tickers"]["cpi"] = cpi
                data["tickers"]["usd_vnd"] = usd_vnd
                data["updated_at"] = vietnam_time

                validate_news_data(data)

                print(
                    f"Groq model {model_name} trả về dữ liệu hợp lệ.",
                    flush=True,
                )
                return data

            except Exception as error:
                detail = f"{type(error).__name__}: {error}"

                # Free tier đôi lúc hết giới hạn token theo phút. Groq thường
                # yêu cầu chờ vài giây, vì vậy thử lại cùng model trước.
                is_rate_limit = "429" in detail or "RateLimit" in detail
                if is_rate_limit and attempt < 3:
                    wait_seconds = attempt * 10
                    print(
                        f"Groq đang giới hạn tạm thời; chờ {wait_seconds} "
                        "giây rồi thử lại...",
                        file=sys.stderr,
                        flush=True,
                    )
                    time.sleep(wait_seconds)
                    continue

                errors.append(f"{model_name}: {detail}")
                print(
                    f"Model {model_name} thất bại: {detail}",
                    file=sys.stderr,
                    flush=True,
                )
                break

    raise RuntimeError(
        "Tất cả Groq model đều thất bại:\n- " + "\n- ".join(errors)
    )


def write_json_atomically(data):
    """Chỉ thay data.json sau khi dữ liệu mới đã hoàn chỉnh."""
    temporary_file = OUTPUT_FILE.with_suffix(".json.tmp")

    with temporary_file.open(
        "w",
        encoding="utf-8",
        newline="\n",
    ) as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write("\n")

    temporary_file.replace(OUTPUT_FILE)


def main():
    api_key = os.environ.get("GROQ_API_KEY", "").strip()

    if not api_key:
        print(
            "LỖI: Không tìm thấy GROQ_API_KEY. "
            "Hãy thêm key tại GitHub Settings > "
            "Secrets and variables > Actions.",
            file=sys.stderr,
        )
        return 1

    try:
        old_tickers = load_previous_tickers()

        cpi = get_value_or_fallback(
            fetch_us_cpi,
            "cpi",
            old_tickers,
        )
        usd_vnd = get_value_or_fallback(
            fetch_usd_vnd,
            "usd_vnd",
            old_tickers,
        )

        client = Groq(
            api_key=api_key,
            default_headers={
                "Groq-Model-Version": "latest",
            },
        )

        data = fetch_news_from_groq(client, cpi, usd_vnd)
        write_json_atomically(data)

        print("Đã cập nhật data.json thành công.", flush=True)
        return 0

    except Exception:
        print(
            "LỖI KHI CẬP NHẬT TIN TỨC:",
            file=sys.stderr,
        )
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
