import csv
import hashlib
import io
import json
import os
import re
import sys
import time
import traceback
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from urllib.parse import quote, quote_plus
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from groq import Groq


# Giữ log tiếng Việt hoạt động khi chạy thủ công trên Windows.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


OUTPUT_FILE = Path(__file__).with_name("data.json")
HISTORY_FILE = Path(__file__).with_name("history.json")
HISTORY_RETENTION_DAYS = 30

REQUIRED_SECTIONS = (
    "macro",
    "vietnam",
    "ai",
    "logistics",
    "gold",
    "silver",
    "stocks",
    "realestate",
)
TREND_SECTIONS = ("macro", "vietnam", "ai", "logistics")
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

TREND_MODELS_TO_TRY = (
    "openai/gpt-oss-20b",
    "llama-3.1-8b-instant",
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
- Các nhóm macro, vietnam, ai, gold, silver, stocks và realestate chọn tối đa
  3 tin đáng chú ý. Riêng logistics chọn tối đa 6 tin, ưu tiên cân bằng ít
  nhất 2 tin Việt Nam và 2 tin thế giới khi danh sách nguồn có đủ tin phù hợp.
- Với logistics, ưu tiên cảng biển, vận tải biển/hàng không/đường sắt, giá cước,
  hành lang thương mại, hạ tầng kho vận, gián đoạn chuỗi cung ứng và chính sách.
- Phải phân biệt rõ dự án đã phê duyệt, đang triển khai, đang nghiên cứu và mới
  chỉ là đề xuất. Không gọi Thailand Land Bridge là "kênh đào Kra"; không suy
  diễn rằng Cần Giờ sẽ nhận toàn bộ hoạt động cảng miền Nam nếu nguồn không nói.
- Không tự ghép quan hệ nhân quả giữa một dự án của Thái Lan và cảng Cần Giờ
  nếu tiêu đề RSS hoặc nguồn tin không đưa ra mối liên hệ đó.

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
      "summary": "tóm tắt 2-3 câu tiếng Việt, nêu rõ địa điểm, trạng thái dự án và tác động logistics nếu tiêu đề nguồn có thông tin",
      "tag": "Logistics Việt Nam hoặc Logistics Thế giới"
    }
  ],
  "gold": [
    {
      "source_index": 0,
      "title": "tiêu đề tiếng Việt",
      "summary": "tóm tắt 2-3 câu tiếng Việt",
      "tag": "Giá vàng / Nhu cầu trú ẩn / Ngân hàng trung ương"
    }
  ],
  "silver": [
    {
      "source_index": 0,
      "title": "tiêu đề tiếng Việt",
      "summary": "tóm tắt 2-3 câu tiếng Việt",
      "tag": "Giá bạc / Nhu cầu công nghiệp"
    }
  ],
  "stocks": [
    {
      "source_index": 0,
      "title": "tiêu đề tiếng Việt",
      "summary": "tóm tắt 2-3 câu tiếng Việt",
      "tag": "VN30 / VN-Index / Cổ phiếu Việt Nam"
    }
  ],
  "realestate": [
    {
      "source_index": 0,
      "title": "tiêu đề tiếng Việt",
      "summary": "tóm tắt 2 câu tiếng Việt",
      "tag": "Căn hộ / Nhà ở / Pháp lý / Hạ tầng"
    }
  ]
}
"""


ONEHOUSING_PROJECTS = (
    {
        "project": "Khang Gia Tân Hương",
        "area": "Tân Phú",
        "tier": "Bình dân",
        "group": "Hạng thường",
        "url": "https://onehousing.vn/phan-tich/du-an/can-ho-chung-cu-du-an-Chung-cu-Khang-Gia-Tan-Huong.500",
        "fallback": (1.69, 21.82, (1.12, 1.99), (20.33, 22.87), 0.0),
    },
    {
        "project": "Melody Residences",
        "area": "Tân Phú",
        "tier": "Trung cấp",
        "group": "Hạng thường",
        "url": "https://onehousing.vn/phan-tich/du-an/can-ho-chung-cu-du-an-Chung-cu-Melody-Residences.713",
        "fallback": (2.99, 43.02, (2.80, 20.65), (31.46, 49.88), 0.0),
    },
    {
        "project": "IDICO Tân Phú",
        "area": "Tân Phú",
        "tier": "Trung cấp",
        "group": "Hạng thường",
        "url": "https://onehousing.vn/phan-tich/du-an/can-ho-chung-cu-du-an-Chung-cu-IDICO-Tan-Phu.493",
        "fallback": (1.84, 35.13, (1.30, 3.38), (31.65, 38.05), 0.0),
    },
    {
        "project": "Q7 Saigon Riverside",
        "area": "Quận 7",
        "tier": "Trung cấp",
        "group": "Hạng thường",
        "url": "https://onehousing.vn/phan-tich/du-an/can-ho-chung-cu-du-an-Chung-cu-Q7-Saigon-Riverside.355",
        "fallback": (2.77, 45.18, (2.06, 3.96), (40.19, 50.22), 0.0),
    },
    {
        "project": "Cảnh Viên 3",
        "area": "Quận 7",
        "tier": "Cao cấp",
        "group": "Hạng sang",
        "url": "https://onehousing.vn/phan-tich/du-an/can-ho-chung-cu-du-an-Canh-Vien-3.644",
        "fallback": (7.17, 61.22, (6.69, 14.81), (57.58, 68.13), 0.0),
    },
    {
        "project": "Vinhomes Grand Park",
        "area": "Thủ Đức · Quận 9 cũ",
        "tier": "Trung cấp – cao cấp",
        "group": "Hạng thường",
        "url": "https://onehousing.vn/phan-tich/du-an/can-ho-chung-cu-du-an-Vinhomes-Grand-Park.1012",
        "fallback": (3.04, 57.53, (1.40, 32.00), (38.59, 331.19), 0.16),
    },
    {
        "project": "Masteri Thảo Điền",
        "area": "Thủ Đức · Quận 2 cũ",
        "tier": "Cao cấp",
        "group": "Hạng sang",
        "url": "https://onehousing.vn/phan-tich/du-an/can-ho-chung-cu-du-an-Masteri-Thao-Dien.946",
        "fallback": (7.96, 120.66, (5.57, 63.11), (100.21, 204.19), -6.99),
    },
    {
        "project": "City Garden",
        "area": "Bình Thạnh",
        "tier": "Cao cấp",
        "group": "Hạng sang",
        "url": "https://onehousing.vn/phan-tich/du-an/can-ho-chung-cu-du-an-Chung-cu-City-Garden.1075",
        "fallback": (12.90, 129.56, (7.40, 21.94), (106.80, 150.74), 0.0),
    },
    {
        "project": "Vinhomes Central Park",
        "area": "Bình Thạnh",
        "tier": "Cao cấp – hạng sang",
        "group": "Hạng sang",
        "url": "https://onehousing.vn/phan-tich/du-an/can-ho-chung-cu-du-an-Vinhomes-Central-Park.813",
        "fallback": (10.73, 144.94, (4.64, 91.62), (104.16, 445.08), 0.0),
    },
)


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


def download_json_post(url, payload):
    """Gửi POST JSON tới nguồn dữ liệu công khai, có tự thử lại."""
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/124 Safari/537.36"
            ),
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": "https://www.tradingview.com",
        },
        method="POST",
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
            time.sleep(attempt * 3)

    raise last_error


def download_html(url):
    """Tải trang HTML công khai phục vụ bản giá BĐS hàng tuần."""
    request = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/124 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.7",
        },
    )
    with urlopen(request, timeout=45) as response:
        return response.read().decode("utf-8", errors="replace")


def html_to_plain_text(html):
    """Loại script, style và thẻ HTML trước khi đọc số liệu."""
    text = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", html, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", unescape(text)).strip()


def parse_number(value):
    """Chuẩn hóa số thập phân trên trang nguồn."""
    return float(value.replace(",", "."))


def fallback_project_snapshot(config, checked_at, source_period="07/2026"):
    """Giữ mốc giá đã kiểm chứng nếu nguồn tạm gián đoạn."""
    price, sqm, price_range, sqm_range, change = config["fallback"]
    return {
        "project": config["project"],
        "area": config["area"],
        "tier": config["tier"],
        "group": config["group"],
        "typical_price_billion": price,
        "typical_price_per_sqm_million": sqm,
        "price_range_billion": list(price_range),
        "sqm_range_million": list(sqm_range),
        "change_pct": change,
        "source": "OneHousing",
        "source_url": config["url"],
        "source_period": source_period,
        "checked_at": checked_at,
        "is_fallback": True,
    }


def fetch_onehousing_project(config, checked_at):
    """Lấy giá phổ biến và đơn giá/m² của một dự án."""
    plain = html_to_plain_text(download_html(config["url"]))
    pattern = re.compile(
        r"Giá phổ biến.*?Mức giá xuất hiện nhiều nhất trong khoảng giá\s*"
        r"([0-9.,]+)\s*tỷ\s*([+-]?[0-9.,]+)%\s*Khoảng giá:\s*"
        r"([0-9.,]+)\s*-\s*([0-9.,]+)\s*tỷ\s*"
        r"Đơn giá phổ biến.*?Mức giá/\s*mét vuông xuất hiện nhiều nhất trong khoảng giá\s*"
        r"([0-9.,]+)\s*triệu/m²\s*([+-]?[0-9.,]+)%\s*Khoảng giá:\s*"
        r"([0-9.,]+)\s*-\s*([0-9.,]+)\s*triệu",
        flags=re.I | re.S,
    )
    match = pattern.search(plain)
    if not match:
        raise ValueError(f"Không đọc được giá {config['project']}.")

    values = [parse_number(value) for value in match.groups()]
    period_match = re.search(r"tháng\s+(\d{1,2}/\d{4})", plain, flags=re.I)
    return {
        "project": config["project"],
        "area": config["area"],
        "tier": config["tier"],
        "group": config["group"],
        "typical_price_billion": values[0],
        "typical_price_per_sqm_million": values[4],
        "price_range_billion": [values[2], values[3]],
        "sqm_range_million": [values[6], values[7]],
        "change_pct": values[1],
        "source": "OneHousing",
        "source_url": config["url"],
        "source_period": period_match.group(1) if period_match else "Chưa rõ",
        "checked_at": checked_at,
        "is_fallback": False,
    }


def build_area_averages(projects):
    """Tính trung bình minh bạch trên rổ dự án đại diện."""
    grouped = {}
    for project in projects:
        grouped.setdefault(project["area"], []).append(project)

    results = []
    for area, items in grouped.items():
        results.append(
            {
                "area": area,
                "sample_size": len(items),
                "average_price_billion": round(
                    sum(item["typical_price_billion"] for item in items) / len(items), 2
                ),
                "average_price_per_sqm_million": round(
                    sum(item["typical_price_per_sqm_million"] for item in items)
                    / len(items),
                    2,
                ),
                "projects": [item["project"] for item in items],
            }
        )
    return results


def fetch_real_estate_market(previous_data):
    """Làm mới rổ giá một lần mỗi ngày, có fallback từng dự án."""
    now = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh"))
    refresh_key = now.strftime("%Y-%m-%d")
    old_market = previous_data.get("real_estate_market", {})
    if (
        isinstance(old_market, dict)
        and old_market.get("refresh_key") == refresh_key
        and old_market.get("apartment_projects")
    ):
        return old_market

    checked_at = now.strftime("%d/%m/%Y %H:%M")
    old_projects = {
        item.get("project"): item
        for item in old_market.get("apartment_projects", [])
        if isinstance(item, dict) and item.get("project")
    }
    projects = []
    for config in ONEHOUSING_PROJECTS:
        try:
            project = fetch_onehousing_project(config, checked_at)
            print(f"Đã lấy giá BĐS: {config['project']}", flush=True)
        except Exception as error:
            project = old_projects.get(config["project"])
            if project:
                project = dict(project)
                project["checked_at"] = checked_at
                project["is_fallback"] = True
            else:
                project = fallback_project_snapshot(config, checked_at)
            print(
                f"CẢNH BÁO: Giữ giá tham chiếu {config['project']}: {error}",
                file=sys.stderr,
                flush=True,
            )
        projects.append(project)

    next_day = now + timedelta(days=1)
    return {
        "refresh_key": refresh_key,
        "updated_at": checked_at,
        "next_update": next_day.strftime("%d/%m/%Y"),
        "methodology": (
            "Trung bình mẫu được tính từ giá phổ biến của các dự án "
            "đại diện trên OneHousing; không phải giá giao dịch công chứng "
            "của toàn bộ khu vực. Hệ thống kiểm tra lại một lần mỗi ngày."
        ),
        "city_benchmark": {
            "label": "Căn hộ sơ cấp khu trung tâm TP.HCM",
            "value_million_per_sqm": 102,
            "change": "-1,2% QoQ",
            "period": "Q1/2026",
            "source": "One Mount Group",
            "source_url": "https://cdn.onehousing.vn/HD/Reports/Reports%20on%20MPI/Bao_cao_Tong_quan_thi_truong_can_ho_TP.HCM_Q1.2026.pdf",
        },
        "area_averages": build_area_averages(projects),
        "apartment_projects": projects,
        "house_ranges": [
            {"area": "Bình Thạnh", "min": 54, "max": 345},
            {"area": "Gò Vấp", "min": 86, "max": 232},
            {"area": "Tân Bình", "min": 87, "max": 330},
            {"area": "Phú Nhuận", "min": 72, "max": 422},
            {"area": "Bình Tân", "min": 47, "max": 227},
            {"area": "Quận 12", "min": 24, "max": 125},
            {"area": "Quận 7", "min": 45, "max": 260},
        ],
        "house_source": {
            "source": "Batdongsan.com.vn – khảo sát tin rao",
            "source_updated": "28/07/2026",
            "url": "https://batdongsan.com.vn/ban-nha-rieng-tp-ho-chi-minh",
            "note": (
                "Khoảng rao bán rất rộng do khác nhau về hẻm/mặt tiền, "
                "diện tích và pháp lý. Mức giữa khoảng chỉ dùng để so sánh nhanh."
            ),
        },
        "land_ranges": [
            {"area": "Quận 2 (cũ)", "min": 62, "max": 608},
            {"area": "Quận 9 (cũ)", "min": 15, "max": 151},
            {"area": "Quận 12", "min": 30, "max": 114},
            {"area": "Thủ Đức (cũ)", "min": 14, "max": 625},
            {"area": "Củ Chi", "min": 2, "max": 33},
        ],
        "land_source": {
            "source": "Batdongsan.com.vn – khảo sát tin rao",
            "source_updated": "09/08/2026",
            "url": "https://batdongsan.com.vn/ban-dat-tp-hcm",
            "note": (
                "Khoảng giá đất chịu ảnh hưởng lớn bởi vị trí, quy hoạch, "
                "mặt tiền và tình trạng pháp lý. Mức giữa khoảng không phải "
                "giá giao dịch trung bình."
            ),
        },
        "disclaimer": (
            "Dữ liệu chỉ để tham khảo, không phải định giá hay khuyến nghị "
            "đầu tư. Trước khi đặt cọc cần kiểm tra pháp lý, quy hoạch, "
            "phí bảo trì, phí quản lý và giá giao dịch thực tế."
        ),
    }


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


def load_json_file(path, fallback):
    """Đọc JSON cũ an toàn để dùng cho lịch sử và dữ liệu dự phòng."""
    if not path.exists():
        return fallback

    try:
        with path.open("r", encoding="utf-8") as file:
            value = json.load(file)
        return value if isinstance(value, dict) else fallback
    except Exception:
        return fallback


def make_article_id(article):
    """Tạo ID ổn định để trình duyệt ghi nhớ bài đã đọc."""
    identity = article.get("url") or (
        f"{article.get('source', '')}|{article.get('title', '')}"
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


def parse_article_time(article):
    """Đưa thời gian RSS hoặc first_seen_at về datetime để sắp xếp."""
    published_at = article.get("published_at", "")
    if published_at:
        try:
            value = parsedate_to_datetime(published_at)
            if value.tzinfo is None:
                value = value.replace(tzinfo=ZoneInfo("UTC"))
            return value
        except (TypeError, ValueError, OverflowError):
            pass

    first_seen_at = article.get("first_seen_at", "")
    try:
        value = datetime.fromisoformat(first_seen_at.replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=ZoneInfo("UTC"))
        return value
    except (TypeError, ValueError):
        return datetime.now(ZoneInfo("UTC"))


def articles_from_news_data(data, first_seen_at):
    """Chuyển data.json thành danh sách phẳng để lưu lịch sử."""
    articles = []
    for section in REQUIRED_SECTIONS:
        for source_article in data.get(section, []):
            if not isinstance(source_article, dict):
                continue
            article = dict(source_article)
            article["section"] = section
            article["first_seen_at"] = article.get(
                "first_seen_at", first_seen_at
            )
            article["id"] = article.get("id") or make_article_id(article)
            articles.append(article)
    return articles


def update_history(current_data, previous_data):
    """Gộp tin mới/cũ, loại trùng và chỉ giữ lại 30 ngày."""
    now = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh"))
    now_iso = now.isoformat(timespec="seconds")
    history_data = load_json_file(HISTORY_FILE, {"articles": []})
    candidates = list(history_data.get("articles", []))
    candidates.extend(articles_from_news_data(previous_data, now_iso))
    candidates.extend(articles_from_news_data(current_data, now_iso))

    unique = {}
    for article in candidates:
        if not isinstance(article, dict) or not article.get("title"):
            continue
        article = dict(article)
        article["id"] = article.get("id") or make_article_id(article)
        article["first_seen_at"] = article.get("first_seen_at", now_iso)
        unique[article["id"]] = article

    cutoff = now - timedelta(days=HISTORY_RETENTION_DAYS)
    retained = [
        article
        for article in unique.values()
        if parse_article_time(article) >= cutoff
    ]
    retained.sort(key=parse_article_time, reverse=True)

    return {
        "updated_at": now.strftime("%d/%m/%Y %H:%M"),
        "retention_days": HISTORY_RETENTION_DAYS,
        "articles": retained,
    }


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


def fetch_yahoo_market(symbol, label, unit, decimals=2):
    """Lấy giá hiện tại và phiên liền trước từ biểu đồ Yahoo Finance."""
    payload = download_json(
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{quote(symbol, safe='')}?range=1mo&interval=1d"
    )
    results = payload.get("chart", {}).get("result") or []
    if not results:
        raise RuntimeError(f"Yahoo Finance không trả về dữ liệu {label}.")

    result = results[0]
    meta = result.get("meta", {})
    current = meta.get("regularMarketPrice")
    if not isinstance(current, (int, float)):
        raise RuntimeError(f"Không tìm thấy giá {label} hiện tại.")

    closes = (
        result.get("indicators", {})
        .get("quote", [{}])[0]
        .get("close", [])
    )
    valid_closes = [
        value for value in closes if isinstance(value, (int, float))
    ]
    previous = valid_closes[-2] if len(valid_closes) >= 2 else None
    if not isinstance(previous, (int, float)):
        previous = meta.get("chartPreviousClose")

    if not isinstance(previous, (int, float)) or previous == 0:
        raise RuntimeError(f"Không tìm thấy giá {label} phiên trước.")

    change = current - previous
    change_pct = change / previous * 100
    direction = "up" if change > 0 else "down" if change < 0 else "flat"
    market_time = meta.get("regularMarketTime")
    if isinstance(market_time, (int, float)):
        checked_at = datetime.fromtimestamp(
            market_time, tz=ZoneInfo("UTC")
        ).astimezone(ZoneInfo("Asia/Ho_Chi_Minh")).strftime(
            "%d/%m/%Y %H:%M"
        )
    else:
        checked_at = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).strftime(
            "%d/%m/%Y %H:%M"
        )

    source_url = f"https://finance.yahoo.com/quote/{quote(symbol, safe='')}"
    return {
        "label": label,
        "value": f"{current:,.{decimals}f}",
        "numeric_value": round(current, decimals + 2),
        "previous_close": f"{previous:,.{decimals}f}",
        "change": f"{change:+,.{decimals}f}",
        "change_pct": f"{change_pct:+.2f}%",
        "direction": direction,
        "unit": unit,
        "source": "Yahoo Finance · COMEX" if symbol.endswith("=F") else "Yahoo Finance",
        "source_url": source_url,
        "updated_at": checked_at,
    }


def fetch_vnindex():
    """Lấy điểm VN-Index và mức thay đổi so với phiên trước."""
    market = fetch_yahoo_market(
        "^VNINDEX.VN", "VN-Index", "điểm", decimals=2
    )

    return {
        "vnindex": market["value"],
        "vnindex_change": market["change"],
        "vnindex_change_pct": market["change_pct"],
        "vnindex_direction": market["direction"],
        "vnindex_previous_close": market["previous_close"],
        "vnindex_updated_at": market["updated_at"],
    }


def fetch_vn30():
    """Lấy VN30 từ TradingView; nguồn công khai trễ khoảng 15 phút."""
    payload = download_json_post(
        "https://scanner.tradingview.com/vietnam/scan",
        {
            "symbols": {
                "tickers": ["HOSE:VN30"],
                "query": {"types": []},
            },
            "columns": [
                "name",
                "description",
                "close",
                "change",
                "change_abs",
                "update_mode",
            ],
        },
    )
    rows = payload.get("data") or []
    values = rows[0].get("d") if rows else None
    if not isinstance(values, list) or len(values) < 5:
        raise RuntimeError("TradingView không trả về dữ liệu VN30.")

    current, change_pct, change = values[2], values[3], values[4]
    if not all(isinstance(value, (int, float)) for value in (current, change_pct, change)):
        raise RuntimeError("Dữ liệu điểm VN30 không hợp lệ.")
    previous = current - change
    direction = "up" if change > 0 else "down" if change < 0 else "flat"
    checked_at = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).strftime(
        "%d/%m/%Y %H:%M"
    )
    return {
        "label": "VN30",
        "value": f"{current:,.2f}",
        "numeric_value": round(current, 4),
        "previous_close": f"{previous:,.2f}",
        "change": f"{change:+,.2f}",
        "change_pct": f"{change_pct:+.2f}%",
        "direction": direction,
        "unit": "điểm",
        "source": "TradingView · dữ liệu trễ khoảng 15 phút",
        "source_url": "https://www.tradingview.com/symbols/HOSE-VN30/",
        "updated_at": checked_at,
    }


def fetch_precious_metal(symbol, label, usd_vnd, metal):
    """Lấy Vàng/Bạc quốc tế và quy đổi tham khảo sang VND."""
    market = fetch_yahoo_market(symbol, label, "USD/oz", decimals=2)
    try:
        rate = float(str(usd_vnd).replace(",", ""))
    except (TypeError, ValueError):
        rate = None
    value = market.get("numeric_value")
    if isinstance(rate, (int, float)) and isinstance(value, (int, float)):
        if metal == "gold":
            # 1 lượng = 37,5 g; 1 troy oz = 31,1034768 g.
            converted = value * rate * (37.5 / 31.1034768) / 1_000_000
            market["vnd_equivalent"] = f"{converted:,.2f} triệu đồng/lượng"
            market["conversion_note"] = (
                "Quy đổi từ giá quốc tế; chưa gồm chênh lệch SJC, thuế và phí."
            )
        else:
            # 1 kg = 32,1507466 troy oz.
            converted = value * rate * 32.1507466 / 1_000_000
            market["vnd_equivalent"] = f"{converted:,.2f} triệu đồng/kg"
            market["conversion_note"] = (
                "Quy đổi từ giá quốc tế; chưa gồm thuế, phí và chênh lệch bán lẻ."
            )
    return market


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


def fetch_google_news_optional(query, language, country, edition, limit=6):
    """Bỏ qua một truy vấn chuyên sâu nếu RSS tạm thời không có kết quả."""
    try:
        return fetch_google_news(query, language, country, edition, limit)
    except Exception as error:
        print(
            f"CẢNH BÁO: Bỏ qua nguồn RSS bổ sung: {error}",
            file=sys.stderr,
            flush=True,
        )
        return []


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
                '("logistics Việt Nam" OR "cảng biển Việt Nam" OR '
                '"chuỗi cung ứng Việt Nam" OR "vận tải hàng hóa" OR '
                '"giá cước vận tải") when:5d',
                "vi",
                "VN",
                "VN:vi",
                limit=6,
            ),
            fetch_google_news(
                '("global logistics" OR "container shipping" OR freight OR '
                '"supply chain disruption" OR "port congestion" OR '
                '"freight rates") when:5d',
                "en-US",
                "US",
                "US:en",
                limit=6,
            ),
            fetch_google_news_optional(
                '("cảng trung chuyển quốc tế Cần Giờ" OR "cảng Cần Giờ" OR '
                '"Cái Mép Thị Vải" OR "cảng Lạch Huyện" OR '
                '"hành lang logistics Việt Nam") when:30d',
                "vi",
                "VN",
                "VN:vi",
                limit=4,
            ),
            fetch_google_news_optional(
                '("Thailand Land Bridge" OR "Kra Canal" OR "Thai Canal" OR '
                '"Southern Economic Corridor Thailand") '
                '(shipping OR port OR logistics OR trade) when:30d',
                "en-US",
                "US",
                "US:en",
                limit=4,
            ),
            fetch_google_news_optional(
                '("Red Sea shipping" OR "Suez Canal" OR "Panama Canal" OR '
                '"Strait of Malacca" OR "South China Sea shipping") when:7d',
                "en-US",
                "US",
                "US:en",
                limit=4,
            ),
            limit=24,
        ),
        "gold": merge_articles(
            fetch_google_news(
                '("giá vàng" OR "vàng SJC" OR "thị trường vàng") when:2d',
                "vi",
                "VN",
                "VN:vi",
                limit=5,
            ),
            fetch_google_news(
                '(gold price OR gold market OR central bank gold) when:2d',
                "en-US",
                "US",
                "US:en",
                limit=4,
            ),
            limit=7,
        ),
        "silver": merge_articles(
            fetch_google_news(
                '("giá bạc" OR "thị trường bạc") when:3d',
                "vi",
                "VN",
                "VN:vi",
                limit=4,
            ),
            fetch_google_news(
                '(silver price OR silver market OR industrial silver) when:3d',
                "en-US",
                "US",
                "US:en",
                limit=5,
            ),
            limit=7,
        ),
        "stocks": fetch_google_news(
            '("VN30" OR "cổ phiếu VN30" OR "thị trường chứng khoán Việt Nam" '
            'OR "VN-Index") when:2d',
            "vi",
            "VN",
            "VN:vi",
            limit=8,
        ),
        "realestate": fetch_google_news(
            '("bất động sản TP.HCM" OR "giá căn hộ TP.HCM" OR '
            '"thị trường nhà ở TP.HCM" OR "pháp lý dự án TP.HCM" OR '
            '"hạ tầng TP.HCM") when:3d',
            "vi",
            "VN",
            "VN:vi",
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
            "vnindex_previous_close": old_tickers.get(
                "vnindex_previous_close", "Chưa có dữ liệu"
            ),
            "vnindex_updated_at": old_tickers.get(
                "vnindex_updated_at", "Chưa rõ"
            ),
        }
        print(
            f"CẢNH BÁO: Không lấy được VN-Index: {error}. "
            "Sử dụng dữ liệu dự phòng.",
            file=sys.stderr,
            flush=True,
        )
        return values


def empty_market_asset(label, unit, source):
    """Cấu trúc dự phòng khi một nguồn giá tạm thời không phản hồi."""
    return {
        "label": label,
        "value": "Chưa có dữ liệu",
        "previous_close": "Chưa có dữ liệu",
        "change": "0.00",
        "change_pct": "0.00%",
        "direction": "flat",
        "unit": unit,
        "source": source,
        "source_url": "",
        "updated_at": "Chưa rõ",
    }


def get_market_asset_or_fallback(fetch_function, key, previous_data, fallback):
    """Giữ snapshot cũ nếu nguồn Vàng/Bạc/VN30 tạm lỗi."""
    try:
        value = fetch_function()
        print(
            f"Đã lấy {value.get('label', key)}: "
            f"{value.get('value')} ({value.get('change_pct')})",
            flush=True,
        )
        return value
    except Exception as error:
        old_value = previous_data.get("market_snapshot", {}).get(key)
        value = old_value if isinstance(old_value, dict) else fallback
        print(
            f"CẢNH BÁO: Không lấy được {key}: {error}. "
            "Sử dụng snapshot dự phòng.",
            file=sys.stderr,
            flush=True,
        )
        return value


def fetch_market_snapshot(previous_data, usd_vnd, vnindex_data):
    """Tạo snapshot các tài sản đầu tư tại mỗi mốc cập nhật."""
    gold = get_market_asset_or_fallback(
        lambda: fetch_precious_metal(
            "GC=F", "Vàng thế giới", usd_vnd, "gold"
        ),
        "gold",
        previous_data,
        empty_market_asset("Vàng thế giới", "USD/oz", "Yahoo Finance · COMEX"),
    )
    silver = get_market_asset_or_fallback(
        lambda: fetch_precious_metal(
            "SI=F", "Bạc thế giới", usd_vnd, "silver"
        ),
        "silver",
        previous_data,
        empty_market_asset("Bạc thế giới", "USD/oz", "Yahoo Finance · COMEX"),
    )
    vn30 = get_market_asset_or_fallback(
        fetch_vn30,
        "vn30",
        previous_data,
        empty_market_asset(
            "VN30", "điểm", "TradingView · dữ liệu trễ khoảng 15 phút"
        ),
    )
    vnindex = {
        "label": "VN-Index",
        "value": vnindex_data.get("vnindex", "Chưa có dữ liệu"),
        "previous_close": vnindex_data.get(
            "vnindex_previous_close", "Chưa có dữ liệu"
        ),
        "change": vnindex_data.get("vnindex_change", "0.00"),
        "change_pct": vnindex_data.get("vnindex_change_pct", "0.00%"),
        "direction": vnindex_data.get("vnindex_direction", "flat"),
        "unit": "điểm",
        "source": "Yahoo Finance",
        "source_url": "https://finance.yahoo.com/quote/%5EVNINDEX.VN/",
        "updated_at": vnindex_data.get("vnindex_updated_at", "Chưa rõ"),
    }
    return {
        "updated_at": datetime.now(
            ZoneInfo("Asia/Ho_Chi_Minh")
        ).strftime("%d/%m/%Y %H:%M"),
        "gold": gold,
        "silver": silver,
        "vnindex": vnindex,
        "vn30": vn30,
        "disclaimer": (
            "Giá Vàng/Bạc là chuẩn quốc tế COMEX; số VND là quy đổi tham khảo, "
            "không phải giá bán lẻ SJC. VN30 có thể trễ khoảng 15 phút."
        ),
    }


def build_investment_overview(data):
    """Tóm tắt tự động toàn bộ nhóm tài sản đầu tư."""
    snapshot = data.get("market_snapshot", {})
    real_estate = data.get("real_estate_market", {})
    gold = snapshot.get("gold", {})
    silver = snapshot.get("silver", {})
    vnindex = snapshot.get("vnindex", {})
    vn30 = snapshot.get("vn30", {})
    benchmark = real_estate.get("city_benchmark", {})

    def movement(asset):
        direction = asset.get("direction")
        verb = "tăng" if direction == "up" else "giảm" if direction == "down" else "đi ngang"
        return f"{verb} {asset.get('change_pct', '0.00%')}"

    property_text = "chưa có dữ liệu mới"
    if benchmark.get("value_million_per_sqm"):
        property_text = (
            f"{benchmark['value_million_per_sqm']:,.0f} triệu đồng/m² "
            f"({benchmark.get('change', 'chưa rõ biến động')})"
        )

    summary = (
        f"Tại mốc {snapshot.get('updated_at', data.get('updated_at', 'hiện tại'))}, "
        f"vàng quốc tế {movement(gold)}, bạc {movement(silver)}; "
        f"VN-Index {movement(vnindex)} và VN30 {movement(vn30)} so với phiên trước. "
        f"Mốc tham khảo căn hộ sơ cấp khu trung tâm TP.HCM hiện ở {property_text}. "
        "Hãy đọc từng chuyên mục để đối chiếu động lực giá, tin mới và rủi ro riêng "
        "của từng loại tài sản."
    )
    return {
        "updated_at": snapshot.get("updated_at", data.get("updated_at", "")),
        "summary": summary,
        "items": [
            {"key": "gold", "label": "Vàng", "status": movement(gold)},
            {"key": "silver", "label": "Bạc", "status": movement(silver)},
            {
                "key": "stocks",
                "label": "Cổ phiếu Việt Nam",
                "status": f"VN30 {movement(vn30)}",
            },
            {
                "key": "realestate",
                "label": "Bất động sản",
                "status": property_text,
            },
        ],
        "disclaimer": "Thông tin nhằm hỗ trợ theo dõi thị trường, không phải khuyến nghị mua bán.",
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
            article["id"] = make_article_id(article)


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
                    max_completion_tokens=4300,
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


def make_empty_trend(label, article_count=0):
    """Tạo trạng thái an toàn khi chưa đủ dữ liệu phân tích."""
    return {
        "label": label,
        "direction": "insufficient",
        "confidence": 0,
        "summary": (
            "Hệ thống đang tích lũy thêm tin trong nhiều ngày để nhận diện "
            "xu hướng đáng tin cậy hơn. Khi kho lịch sử đủ rộng, phần này "
            "sẽ giải thích rõ diễn biến hiện tại, các động lực chính và những "
            "kịch bản cần theo dõi."
        ),
        "short_term": {
            "horizon": "1-4 tuần",
            "outlook": "uncertain",
            "summary": "Chưa đủ dữ liệu để xác định xu hướng ngắn hạn.",
        },
        "long_term": {
            "horizon": "3-12 tháng",
            "outlook": "uncertain",
            "summary": (
                "Chưa đủ dữ liệu để xây dựng kịch bản dài hạn có căn cứ."
            ),
        },
        "drivers": [],
        "watch_next": [],
        "article_count": article_count,
    }


def make_fallback_trends(history):
    """Không để lỗi AI làm gián đoạn cập nhật tin chính."""
    counts = {
        section: sum(
            1
            for article in history.get("articles", [])
            if article.get("section") == section
        )
        for section in TREND_SECTIONS
    }
    labels = {
        "macro": "Vĩ mô",
        "vietnam": "Việt Nam",
        "ai": "Trí tuệ nhân tạo",
        "logistics": "Logistics",
    }
    return {
        "generated_at": history.get("updated_at", ""),
        "window_days": 7,
        "overall": make_empty_trend(
            "Toàn cảnh", sum(counts.values())
        ),
        "sections": {
            section: make_empty_trend(labels[section], counts[section])
            for section in TREND_SECTIONS
        },
        "disclaimer": (
            "Phân tích do AI tổng hợp từ các bài báo đã lưu, "
            "không phải tư vấn đầu tư."
        ),
    }


def validate_trends(trends, valid_article_ids):
    """Kiểm tra trend có đủ cấu trúc và chỉ dẫn chứng bài tồn tại."""
    if not isinstance(trends, dict):
        raise ValueError("Trend không phải JSON object.")

    sections = trends.get("sections")
    if not isinstance(sections, dict):
        raise ValueError("Trend thiếu mục sections.")

    trend_items = [trends.get("overall")]
    trend_items.extend(sections.get(section) for section in TREND_SECTIONS)

    for trend in trend_items:
        if not isinstance(trend, dict):
            raise ValueError("Một mục trend không hợp lệ.")
        if not isinstance(trend.get("summary"), str) or not trend["summary"].strip():
            raise ValueError("Trend thiếu summary.")
        if trend.get("direction") not in {
            "up", "down", "mixed", "stable", "insufficient"
        }:
            raise ValueError("Trend có direction không hợp lệ.")
        confidence = trend.get("confidence")
        if not isinstance(confidence, (int, float)):
            raise ValueError("Trend thiếu confidence.")
        trend["confidence"] = max(0, min(100, round(confidence)))
        trend.setdefault("drivers", [])
        trend.setdefault("watch_next", [])

        horizon_defaults = {
            "short_term": (
                "1-4 tuần",
                "Trong 1-4 tuần, tín hiệu hiện tại vẫn cần được "
                "kiểm chứng bằng các bản tin mới. Kịch bản này được "
                "suy ra từ các động lực vừa nêu, nhưng có thể thay đổi "
                "khi xuất hiện dữ liệu hoặc sự kiện mới.",
            ),
            "long_term": (
                "3-12 tháng",
                "Trong 3-12 tháng, triển vọng phụ thuộc vào việc các "
                "động lực trong bản tin có tiếp tục duy trì hay không. Vì "
                "kho phân tích hiện chỉ bao phủ bảy ngày, đây là kịch bản "
                "cơ sở chứ không phải dự báo chắc chắn. Những dữ liệu, "
                "chính sách hoặc sự kiện mới có thể làm thay đổi đánh giá.",
            ),
        }
        for horizon_name in ("short_term", "long_term"):
            horizon = trend.get(horizon_name)
            if not isinstance(horizon, dict):
                fallback_horizon, fallback_summary = horizon_defaults[horizon_name]
                horizon = {
                    "horizon": fallback_horizon,
                    "outlook": "uncertain",
                    "summary": fallback_summary,
                }
                trend[horizon_name] = horizon
            if horizon.get("outlook") not in {
                "positive", "negative", "mixed", "stable", "uncertain"
            }:
                horizon["outlook"] = "uncertain"
            if not isinstance(horizon.get("summary"), str) or not horizon["summary"].strip():
                horizon["summary"] = horizon_defaults[horizon_name][1]

        for driver in trend["drivers"]:
            if not isinstance(driver, dict):
                raise ValueError("Driver của trend không hợp lệ.")
            references = driver.get("article_ids", [])
            driver["article_ids"] = [
                article_id
                for article_id in references
                if article_id in valid_article_ids
            ]


def fetch_trends_from_groq(client, history):
    """Phân tích xu hướng bảy ngày từ kho tin đã lưu."""
    now = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh"))
    cutoff = now - timedelta(days=7)
    recent_by_section = {section: [] for section in TREND_SECTIONS}

    for article in history.get("articles", []):
        section = article.get("section")
        if section not in recent_by_section or parse_article_time(article) < cutoff:
            continue
        if len(recent_by_section[section]) >= 12:
            continue
        recent_by_section[section].append(
            {
                "id": article.get("id"),
                "title": article.get("title"),
                "summary": article.get("summary"),
                "source": article.get("source"),
                "published_at": article.get("published_at"),
            }
        )

    valid_article_ids = {
        article["id"]
        for articles in recent_by_section.values()
        for article in articles
        if article.get("id")
    }
    if len(valid_article_ids) < 8:
        return make_fallback_trends(history)

    prompt = f"""
Bạn là chuyên gia phân tích xu hướng tin tức của The Daily Edge.

Chỉ được dùng dữ liệu 7 ngày dưới đây. Không thêm sự kiện, số liệu hoặc
kết luận không được hỗ trợ bởi các bài đã cung cấp. Phân tích bằng tiếng Việt,
rõ ràng, dễ đọc và có mạch lập luận. Đây là phân tích thông tin, tuyệt đối
không đưa khuyến nghị mua/bán đầu tư.

Quy ước thời gian:
- Ngắn hạn: 1-4 tuần tiếp theo, bám sát tín hiệu đang xuất hiện trong tin.
- Dài hạn: 3-12 tháng, chỉ mô tả kịch bản và điều kiện có thể dẫn tới kịch bản;
  không khẳng định chắc chắn tương lai.

Dữ liệu:
{json.dumps(recent_by_section, ensure_ascii=False)}

Trả về duy nhất một JSON object theo cấu trúc:
{{
  "overall": {{
    "label": "Toàn cảnh",
    "direction": "up|down|mixed|stable|insufficient",
    "confidence": 0,
    "summary": "4-6 câu giải thích bức tranh chung và mối liên hệ giữa các tín hiệu",
    "short_term": {{
      "horizon": "1-4 tuần",
      "outlook": "positive|negative|mixed|stable|uncertain",
      "summary": "3-4 câu, nêu hướng đi có khả năng nhất và điều kiện làm thay đổi nó"
    }},
    "long_term": {{
      "horizon": "3-12 tháng",
      "outlook": "positive|negative|mixed|stable|uncertain",
      "summary": "3-4 câu, nêu kịch bản cơ sở, cơ hội và rủi ro"
    }},
    "drivers": [{{"text": "động lực", "article_ids": ["id"]}}],
    "watch_next": ["2-3 điều cụ thể cần theo dõi"]
  }},
  "sections": {{
    "macro": {{"label": "Vĩ mô", "direction": "mixed", "confidence": 0,
      "summary": "4-6 câu", "short_term": {{"horizon": "1-4 tuần", "outlook": "mixed", "summary": "3-4 câu"}},
      "long_term": {{"horizon": "3-12 tháng", "outlook": "mixed", "summary": "3-4 câu"}}, "drivers": [], "watch_next": []}},
    "vietnam": {{"label": "Việt Nam", "direction": "mixed", "confidence": 0,
      "summary": "4-6 câu", "short_term": {{"horizon": "1-4 tuần", "outlook": "mixed", "summary": "3-4 câu"}},
      "long_term": {{"horizon": "3-12 tháng", "outlook": "mixed", "summary": "3-4 câu"}}, "drivers": [], "watch_next": []}},
    "ai": {{"label": "Trí tuệ nhân tạo", "direction": "mixed", "confidence": 0,
      "summary": "4-6 câu", "short_term": {{"horizon": "1-4 tuần", "outlook": "mixed", "summary": "3-4 câu"}},
      "long_term": {{"horizon": "3-12 tháng", "outlook": "mixed", "summary": "3-4 câu"}}, "drivers": [], "watch_next": []}},
    "logistics": {{"label": "Logistics", "direction": "mixed", "confidence": 0,
      "summary": "4-6 câu", "short_term": {{"horizon": "1-4 tuần", "outlook": "mixed", "summary": "3-4 câu"}},
      "long_term": {{"horizon": "3-12 tháng", "outlook": "mixed", "summary": "3-4 câu"}}, "drivers": [], "watch_next": []}}
  }}
}}

confidence là số nguyên 0-100. Mỗi driver phải dẫn article_ids có thật.
Mỗi summary phải cụ thể, tránh câu chung chung. Nếu dữ liệu chưa đủ, dùng
direction "insufficient", outlook "uncertain" và nói rõ phần nào còn thiếu.
"""

    errors = []
    for model_name in TREND_MODELS_TO_TRY:
        try:
            print(f"Đang phân tích trend với {model_name}...", flush=True)
            response = client.chat.completions.create(
                model=model_name,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Bạn chỉ trả về JSON hợp lệ và luôn dẫn chứng "
                            "bằng article_ids được cung cấp."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                # Groq Free tính cả prompt + phần trả lời vào giới hạn
                # 8.000 TPM của model này. 2.700 token vẫn đủ cho
                # JSON chi tiết, đồng thời chừa khoảng an toàn cho input.
                max_completion_tokens=2700,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content
            if not content:
                raise ValueError("Groq trả về trend rỗng.")
            trends = extract_json(content)
            validate_trends(trends, valid_article_ids)
            trends["generated_at"] = now.strftime("%d/%m/%Y %H:%M")
            trends["window_days"] = 7
            trends["disclaimer"] = (
                "Phân tích do AI tổng hợp từ các bài báo đã lưu, "
                "không phải tư vấn đầu tư."
            )

            counts = {
                section: len(articles)
                for section, articles in recent_by_section.items()
            }
            trends["overall"]["article_count"] = sum(counts.values())
            for section in TREND_SECTIONS:
                trends["sections"][section]["article_count"] = counts[section]
            return trends
        except Exception as error:
            errors.append(f"{model_name}: {type(error).__name__}: {error}")
            print(
                f"CẢNH BÁO: Phân tích trend thất bại với {model_name}: {error}",
                file=sys.stderr,
                flush=True,
            )

    raise RuntimeError("Không thể tạo trend:\n- " + "\n- ".join(errors))


def write_json_atomically(data, output_file=OUTPUT_FILE):
    """Chỉ thay tệp JSON sau khi dữ liệu mới đã hoàn chỉnh."""
    temporary_file = output_file.with_suffix(".json.tmp")

    with temporary_file.open(
        "w",
        encoding="utf-8",
        newline="\n",
    ) as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write("\n")

    temporary_file.replace(output_file)


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
        previous_data = load_json_file(OUTPUT_FILE, {})
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
        market_snapshot = fetch_market_snapshot(
            previous_data,
            usd_vnd,
            vnindex_data,
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
            vnindex_data,
            news_sources,
        )
        data["market_snapshot"] = market_snapshot
        data["real_estate_market"] = fetch_real_estate_market(previous_data)
        data["investment_overview"] = build_investment_overview(data)
        history = update_history(data, previous_data)

        try:
            data["trends"] = fetch_trends_from_groq(client, history)
        except Exception as trend_error:
            old_trends = previous_data.get("trends")
            old_items = []
            if isinstance(old_trends, dict) and isinstance(old_trends.get("sections"), dict):
                old_items = [old_trends.get("overall")]
                old_items.extend(
                    old_trends["sections"].get(section)
                    for section in TREND_SECTIONS
                )
            old_trends_have_horizons = bool(old_items) and all(
                isinstance(item, dict)
                and isinstance(item.get("short_term"), dict)
                and isinstance(item.get("long_term"), dict)
                for item in old_items
            )
            if old_trends_have_horizons:
                data["trends"] = old_trends
                print(
                    f"CẢNH BÁO: Giữ trend cũ do lỗi: {trend_error}",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                data["trends"] = make_fallback_trends(history)

        write_json_atomically(data)
        write_json_atomically(history, HISTORY_FILE)

        print(
            "Đã cập nhật data.json và history.json thành công.",
            flush=True,
        )
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
