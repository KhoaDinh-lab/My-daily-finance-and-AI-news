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

REQUIRED_SECTIONS = ("macro", "vietnam", "ai", "logistics")
REQUIRED_TICKERS = (
    "fed_rate",
    "cpi",
    "vnindex",
    "vnindex_change",
    "vnindex_change_pct",
    "vnindex_direction",
    "usd_vnd",
)
REQUIRED_NEWS_FIELDS = (
    "title",
    "summary",
    "source",
    "tag",
    "url",
    "published_at",
)

# Tin mới được lấy từ RSS; Groq chỉ biên tập và tóm tắt.
MODELS_TO_TRY = (
    "llama-3.3-70b-versatile",
    "openai/gpt-oss-20b",
)


SYSTEM_PROMPT = """
Bạn là biên tập viên của website tin tức The Daily Edge.

Nhiệm vụ:
- Chọn và biên tập tin từ danh sách RSS do chương trình cung cấp.
- Không được tự bịa tiêu đề, số liệu, nguồn tin hoặc sự kiện.
- Không thêm chi tiết không có trong tiêu đề RSS.
- Dịch title sang tiếng Việt tự nhiên, nhưng không làm thay đổi ý nghĩa.
- Mọi title, summary và tag PHẢI viết bằng tiếng Việt.
- Giữ đúng source_index của tin RSS được chọn.
- Nếu chưa tìm được một số liệu đáng tin cậy, ghi "Chưa có dữ liệu".
- Mỗi nhóm chọn tối đa 4 tin đáng chú ý, giữ nguyên thứ tự theo source_index.

Chỉ trả về một JSON object hợp lệ, không dùng Markdown và không thêm
lời giải thích bên ngoài JSON.

Cấu trúc bắt buộc:

{
  "updated_at": "thời gian cập nhật",
  "tickers": {
    "fed_rate": "giá trị hoặc Chưa có dữ liệu",
    "cpi": "giá trị",
    "vnindex": "giá trị",
    "usd_vnd": "giá trị"
  },
  "macro": [
    {
      "source_index": 0,
      "title": "tiêu đề tiếng Việt",
      "summary": "tóm tắt 2 câu tiếng Việt",
      "tag": "chủ đề tiếng Việt"
    }
  ],
  "vietnam": [
    {
      "source_index": 0,
      "title": "tiêu đề tiếng Việt",
      "summary": "tóm tắt 2 câu tiếng Việt",
      "tag": "chủ đề tiếng Việt"
    }
  ],
  "ai": [
    {
      "source_index": 0,
      "title": "tiêu đề tiếng Việt",
      "summary": "tóm tắt 2 câu tiếng Việt",
      "tag": "chủ đề tiếng Việt"
    }
  ],
  "logistics": [
    {
      "source_index": 0,
      "title": "tiêu đề tiếng Việt",
      "summary": "tóm tắt 2 câu tiếng Việt",
      "tag": "Logistics Việt Nam hoặc Logistics Thế giới"
    }
  ]
}
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


def fetch_vnindex():
    """Lấy điểm VN-Index và mức thay đổi ngày từ Yahoo Finance."""
    payload = download_json(
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        "%5EVNINDEX.VN?range=1mo&interval=1d"
    )
    results = payload.get("chart", {}).get("result") or []
    if not results:
        raise RuntimeError("Yahoo Finance không trả về dữ liệu VN-Index.")

    result = results[0]
    meta = result.get("meta", {})
    current = meta.get("regularMarketPrice")
    previous = meta.get("chartPreviousClose")

    if not isinstance(current, (int, float)):
        raise RuntimeError("Không tìm thấy điểm VN-Index hiện tại.")

    if not isinstance(previous, (int, float)):
        closes = (
            result.get("indicators", {})
            .get("quote", [{}])[0]
            .get("close", [])
        )
        valid_closes = [value for value in closes if isinstance(value, (int, float))]
        if len(valid_closes) >= 2:
            previous = valid_closes[-2]

    if not isinstance(previous, (int, float)) or previous == 0:
        raise RuntimeError("Không tìm thấy điểm VN-Index phiên trước.")

    change = current - previous
    change_pct = change / previous * 100
    direction = "up" if change > 0 else "down" if change < 0 else "flat"

    return {
        "vnindex": f"{current:,.2f}",
        "vnindex_change": f"{change:+,.2f}",
        "vnindex_change_pct": f"{change_pct:+.2f}%",
        "vnindex_direction": direction,
    }


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
        article_url = clean_rss_text(item.findtext("link"))

        if title:
            articles.append(
                {
                    "title": title,
                    "source": source or "Google News",
                    "published_at": published_at,
                    "url": article_url,
                }
            )

    if not articles:
        raise RuntimeError(f"RSS không có kết quả cho truy vấn: {query}")

    return articles


def merge_articles(*article_groups, limit=8):
    """Gộp nhiều RSS và loại tiêu đề trùng nhau."""
    merged = []
    seen_titles = set()

    for group in article_groups:
        for article in group:
            key = article["title"].casefold()
            if key in seen_titles:
                continue
            seen_titles.add(key)
            merged.append(article)
            if len(merged) >= limit:
                return merged

    return merged


def fetch_news_sources():
    """Thu thập các nhóm tiêu đề mới trước khi gửi sang Groq."""
    sources = {
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
        "logistics": merge_articles(
            fetch_google_news(
                '("logistics Việt Nam" OR "cảng biển" OR '
                '"chuỗi cung ứng" OR "vận tải hàng hóa") when:3d',
                "vi",
                "VN",
                "VN:vi",
                limit=5,
            ),
            fetch_google_news(
                '("global logistics" OR shipping OR freight OR '
                '"supply chain") when:3d',
                "en-US",
                "US",
                "US:en",
                limit=5,
            ),
            limit=8,
        ),
    }

    for articles in sources.values():
        for source_index, article in enumerate(articles):
            article["source_index"] = source_index

    return sources


def get_vnindex_or_fallback(old_tickers):
    """Giữ dữ liệu VN-Index cũ nếu Yahoo Finance tạm thời bị lỗi."""
    try:
        values = fetch_vnindex()
        print(
            "Đã lấy VN-Index: "
            f"{values['vnindex']} ({values['vnindex_change_pct']})",
            flush=True,
        )
        return values
    except Exception as error:
        values = {
            "vnindex": old_tickers.get("vnindex", "Chưa có dữ liệu"),
            "vnindex_change": old_tickers.get("vnindex_change", "0.00"),
            "vnindex_change_pct": old_tickers.get(
                "vnindex_change_pct", "0.00%"
            ),
            "vnindex_direction": old_tickers.get(
                "vnindex_direction", "flat"
            ),
        }
        print(
            f"CẢNH BÁO: Không lấy được VN-Index: {error}. "
            "Sử dụng dữ liệu dự phòng.",
            file=sys.stderr,
            flush=True,
        )
        return values


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


def attach_source_metadata(data, news_sources):
    """Gắn nguồn, link và thời gian thật từ RSS vào bài Groq đã chọn."""
    for section in REQUIRED_SECTIONS:
        source_articles = news_sources.get(section, [])
        selected_articles = data.get(section, [])

        if not isinstance(selected_articles, list):
            raise ValueError(f"Mục {section} không phải danh sách.")

        for position, article in enumerate(selected_articles, start=1):
            source_index = article.pop("source_index", None)
            if not isinstance(source_index, int):
                raise ValueError(
                    f"{section}[{position}] thiếu source_index hợp lệ."
                )
            if source_index < 0 or source_index >= len(source_articles):
                raise ValueError(
                    f"{section}[{position}] có source_index ngoài phạm vi."
                )

            original = source_articles[source_index]
            article["source"] = original["source"]
            article["url"] = original["url"]
            article["published_at"] = original["published_at"]


def fetch_news_from_groq(client, cpi, usd_vnd, vnindex_data, news_sources):
    vietnam_time = datetime.now(
        ZoneInfo("Asia/Ho_Chi_Minh")
    ).strftime("%d/%m/%Y %H:%M")

    compact_sources = {
        section: [
            {
                "source_index": article["source_index"],
                "title": article["title"],
                "source": article["source"],
                "published_at": article["published_at"],
            }
            for article in articles
        ]
        for section, articles in news_sources.items()
    }

    user_prompt = f"""
Thời gian hiện tại tại Việt Nam: {vietnam_time}.

Hãy biên tập bản tin từ đúng danh sách tiêu đề RSS dưới đây.
Không tự tìm thêm hoặc thêm chi tiết không có trong tiêu đề.

Các số liệu đã được lấy trực tiếp từ API dữ liệu:
- CPI Mỹ theo năm: {cpi}
- Tỷ giá tham khảo USD/VND: {usd_vnd}
- VN-Index: {vnindex_data['vnindex']}
- Thay đổi VN-Index: {vnindex_data['vnindex_change']} ({vnindex_data['vnindex_change_pct']})

Không tự thay đổi các số liệu trên.

Đối với fed_rate:
- Chỉ điền số liệu nếu tìm được nguồn mới và đáng tin cậy.
- Nếu không chắc chắn, ghi "Chưa có dữ liệu".

Danh sách tiêu đề RSS:
{json.dumps(compact_sources, ensure_ascii=False)}

Nhắc lại: mọi title, summary và tag bắt buộc hoàn toàn bằng tiếng Việt.
Giữ nguyên source_index của từng tin được chọn.
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

                # Luôn dùng dữ liệu trực tiếp từ API cho các chỉ số này.
                data.setdefault("tickers", {})
                data["tickers"]["cpi"] = cpi
                data["tickers"]["usd_vnd"] = usd_vnd
                data["tickers"].update(vnindex_data)
                data["updated_at"] = vietnam_time

                attach_source_metadata(data, news_sources)
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
        vnindex_data = get_vnindex_or_fallback(old_tickers)
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
            vnindex_data,
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
