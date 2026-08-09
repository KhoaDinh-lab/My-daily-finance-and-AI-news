import csv
import io
import json
import os
import re
import sys
import time
import traceback
import xml.etree.ElementTree as ET
from datetime import datetime
from html import unescape
from pathlib import Path
from urllib.parse import quote_plus
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from groq import Groq


OUTPUT_FILE = Path(__file__).with_name("data.json")

REQUIRED_SECTIONS = ("macro", "vietnam", "ai")
REQUIRED_TICKERS = ("fed_rate", "cpi", "vnindex", "usd_vnd")
REQUIRED_NEWS_FIELDS = ("title", "summary", "source", "tag")

# Tin mới được lấy từ RSS; Groq chỉ biên tập và tóm tắt.
MODELS_TO_TRY = (
    "llama-3.3-70b-versatile",
    "openai/gpt-oss-20b",
)


SYSTEM_PROMPT = """
Bạn là biên tập viên của website tin tức The Daily Edge.

Nhiệm vụ:
- Biên tập danh sách tin RSS mới trong 24-48 giờ do chương trình cung cấp.
- Ưu tiên nguồn chính thức hoặc báo chí uy tín.
- Không được tự bịa tiêu đề, số liệu, nguồn tin hoặc sự kiện.
- Không thêm sự kiện hoặc số liệu không có trong dữ liệu đầu vào.
- Giữ nguyên title gốc nhưng mọi summary và tag PHẢI viết bằng tiếng Việt.
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


def download_text(url):
    """Tải văn bản từ một API hoặc RSS công khai, có tự thử lại."""
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
                return response.read().decode("utf-8")
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


def download_json(url):
    """Tải và đọc JSON từ một API công khai."""
    return json.loads(download_text(url))


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


def fetch_us_cpi_from_bls():
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


def fetch_us_cpi_from_fred():
    """Nguồn dự phòng CPI từ Federal Reserve Bank of St. Louis."""
    csv_text = download_text(
        "https://fred.stlouisfed.org/graph/fredgraph.csv?id=CPIAUCSL"
    )
    monthly_values = {}

    for row in csv.DictReader(io.StringIO(csv_text)):
        date_text = row.get("DATE") or row.get("observation_date")
        value_text = row.get("CPIAUCSL")
        if not date_text or not value_text or value_text == ".":
            continue

        date_value = datetime.strptime(date_text, "%Y-%m-%d")
        monthly_values[(date_value.year, date_value.month)] = float(value_text)

    if not monthly_values:
        raise RuntimeError("FRED không trả về dữ liệu CPI theo tháng.")

    latest_year, latest_month = max(monthly_values)
    latest_value = monthly_values[(latest_year, latest_month)]
    previous_value = monthly_values.get((latest_year - 1, latest_month))
    if previous_value is None:
        raise RuntimeError("FRED không đủ dữ liệu để tính CPI cùng kỳ.")

    inflation = (latest_value / previous_value - 1) * 100
    return f"{inflation:.1f}%"


def fetch_us_cpi():
    """Ưu tiên BLS và tự chuyển sang FRED nếu BLS tạm lỗi."""
    try:
        return fetch_us_cpi_from_bls()
    except Exception as error:
        print(
            f"BLS tạm lỗi ({error}); chuyển sang nguồn FRED...",
            file=sys.stderr,
            flush=True,
        )
        return fetch_us_cpi_from_fred()


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


def clean_rss_text(value):
    """Bỏ thẻ HTML và khoảng trắng thừa trong RSS."""
    text = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", unescape(text)).strip()


def fetch_google_news(query, language, country, edition, limit=6):
    """Lấy tiêu đề mới từ Google News RSS, không cần API key."""
    url = (
        "https://news.google.com/rss/search?q="
        f"{quote_plus(query)}&hl={language}&gl={country}&ceid={edition}"
    )
    root = ET.fromstring(download_text(url))
    articles = []

    for item in root.findall("./channel/item")[:limit]:
        title = clean_rss_text(item.findtext("title"))
        source_element = item.find("source")
        source = clean_rss_text(
            source_element.text if source_element is not None else ""
        )
        published_at = clean_rss_text(item.findtext("pubDate"))

        if title:
            articles.append(
                {
                    "title": title,
                    "source": source or "Google News",
                    "published_at": published_at,
                }
            )

    if not articles:
        raise RuntimeError(f"RSS không có kết quả cho truy vấn: {query}")

    return articles


def fetch_news_sources():
    """Thu thập ba nhóm tiêu đề mới trước khi gửi sang Groq."""
    return {
        "macro": fetch_google_news(
            "(Federal Reserve OR Fed OR US CPI OR US inflation) when:2d",
            "en-US",
            "US",
            "US:en",
        ),
        "vietnam": fetch_google_news(
            '("VN-Index" OR "USD/VND" OR NHNN OR "FDI Việt Nam") when:2d',
            "vi",
            "VN",
            "VN:vi",
        ),
        "ai": fetch_google_news(
            "(OpenAI OR Anthropic OR Gemini OR AI model OR AI chip) when:2d",
            "en-US",
            "US",
            "US:en",
        ),
    }


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


def fetch_news_from_groq(client, cpi, usd_vnd, news_sources):
    vietnam_time = datetime.now(
        ZoneInfo("Asia/Ho_Chi_Minh")
    ).strftime("%d/%m/%Y %H:%M")

    user_prompt = f"""
Thời gian hiện tại tại Việt Nam: {vietnam_time}.

Hãy biên tập bản tin từ đúng danh sách tiêu đề RSS dưới đây.
Không tự tìm thêm hoặc thêm chi tiết không có trong tiêu đề.

Hai số liệu đã được lấy trực tiếp từ API dữ liệu:
- CPI Mỹ theo năm: {cpi}
- Tỷ giá tham khảo USD/VND: {usd_vnd}

Không tự thay đổi hai số liệu trên.

Đối với fed_rate và vnindex:
- Chỉ điền số liệu nếu tìm được nguồn mới và đáng tin cậy.
- Nếu không chắc chắn, ghi "Chưa có dữ liệu".

Danh sách tiêu đề RSS:
{json.dumps(news_sources, ensure_ascii=False)}

Nhắc lại: mọi trường summary và tag bắt buộc hoàn toàn bằng tiếng Việt.
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
                    max_completion_tokens=2200,
                    response_format={"type": "json_object"},
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
        news_sources = fetch_news_sources()
        print(
            "Đã lấy RSS: "
            + ", ".join(
                f"{section}={len(items)}"
                for section, items in news_sources.items()
            ),
            flush=True,
        )

        client = Groq(
            api_key=api_key,
            default_headers={
                "Groq-Model-Version": "latest",
            },
        )

        data = fetch_news_from_groq(
            client,
            cpi,
            usd_vnd,
            news_sources,
        )
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
