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

POOL_FILE = Path(__file__).with_name("pool.json")
POOL_RETENTION_DAYS = 14

# Chuyên mục chỉ đi vào pool.json để phục vụ danh mục cá nhân hoá.
# Không hiển thị trên bản tin chung và KHÔNG gửi cho Groq.
POOL_ONLY_SECTIONS = (
    "semiconductor",
    "energy",
    "trade_policy",
    "fintech_vn",
)

# Số bài RSS tối đa gửi cho Groq ở mỗi chuyên mục.
# Pool lấy nhiều hơn hẳn, nhưng phần dư KHÔNG bao giờ được gửi cho Groq:
# gói Free chỉ cho 8.000 token/phút nên mở rộng kho tin phải miễn phí về token.
GROQ_SAMPLE_LIMITS = {
    "macro": 6,
    "vietnam": 6,
    "ai": 6,
    "logistics": 20,
    "gold": 7,
    "silver": 7,
    "stocks": 8,
    "realestate": 8,
}

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
# 24/09/2026: Groq đã chuyển llama-3.3-70b-versatile và llama-3.1-8b-instant
# sang gói Enterprise nên key gói Free nhận lỗi 404 model_not_found.
# Hai model gpt-oss dưới đây nằm trong gói Free (30 RPM, 1K RPD, 8K TPM).
MODELS_TO_TRY = (
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
)

TREND_MODELS_TO_TRY = (
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
)

# Gói Free giới hạn 8.000 token/phút. Gửi cả 8 chuyên mục trong một request
# tốn ~11.000 token nên chắc chắn dính 429. Vì vậy chia thành 3 lô nhỏ và
# nghỉ giữa các lô để sang cửa sổ giới hạn mới.
SECTION_BATCHES = (
    ("macro", "vietnam", "ai"),
    ("logistics",),
    ("gold", "silver", "stocks", "realestate"),
)

SLEEP_BETWEEN_BATCHES = 65
MAX_COMPLETION_TOKENS_PER_BATCH = 4000


SECTION_RULES = {
    "macro": "- macro: tối đa 3 tin về CPI, lãi suất FED và kinh tế Mỹ.",
    "vietnam": "- vietnam: tối đa 3 tin về VN-Index, USD/VND, NHNN, FDI.",
    "ai": "- ai: tối đa 3 tin về model mới, agentic AI, chip AI.",
    "logistics": (
        "- logistics: tối đa 6 tin, ưu tiên cân bằng ít nhất 2 tin Việt Nam\n"
        "  và 2 tin thế giới khi danh sách nguồn có đủ tin phù hợp.\n"
        "- Ưu tiên cảng biển, vận tải biển/hàng không/đường sắt, giá cước,\n"
        "  hành lang thương mại, hạ tầng kho vận, gián đoạn chuỗi cung ứng\n"
        "  và chính sách.\n"
        "- Phải phân biệt rõ dự án đã phê duyệt, đang triển khai, đang nghiên\n"
        "  cứu và mới chỉ là đề xuất. Không gọi Thailand Land Bridge là\n"
        "  \"kênh đào Kra\"; không suy diễn rằng Cần Giờ sẽ nhận toàn bộ hoạt\n"
        "  động cảng miền Nam nếu nguồn không nói.\n"
        "- Không tự ghép quan hệ nhân quả giữa một dự án của Thái Lan và cảng\n"
        "  Cần Giờ nếu tiêu đề RSS hoặc nguồn tin không đưa ra mối liên hệ đó."
    ),
    "gold": "- gold: tối đa 3 tin về giá vàng, nhu cầu trú ẩn, ngân hàng trung ương.",
    "silver": "- silver: tối đa 3 tin về giá bạc và nhu cầu công nghiệp.",
    "stocks": "- stocks: tối đa 3 tin về VN30, VN-Index, cổ phiếu Việt Nam.",
    "realestate": "- realestate: tối đa 3 tin về căn hộ, nhà ở, pháp lý, hạ tầng.",
}

SECTION_TAG_HINT = {
    "macro": "Vĩ mô / FED / Lạm phát",
    "vietnam": "Việt Nam / Tỷ giá / Thị trường",
    "ai": "Trí tuệ nhân tạo / Chip AI",
    "logistics": "Logistics Việt Nam hoặc Logistics Thế giới",
    "gold": "Giá vàng / Nhu cầu trú ẩn / Ngân hàng trung ương",
    "silver": "Giá bạc / Nhu cầu công nghiệp",
    "stocks": "VN30 / VN-Index / Cổ phiếu Việt Nam",
    "realestate": "Căn hộ / Nhà ở / Pháp lý / Hạ tầng",
}

SECTION_SUMMARY_HINT = {
    "logistics": (
        "tóm tắt 2-3 câu tiếng Việt, nêu rõ địa điểm, trạng thái dự án và "
        "tác động logistics nếu tiêu đề nguồn có thông tin"
    ),
}
DEFAULT_SUMMARY_HINT = "tóm tắt 2-3 câu tiếng Việt"

BASE_RULES = """
Bạn là biên tập viên của website tin tức The Daily Edge.

Nhiệm vụ:
- Chọn và biên tập tin từ danh sách RSS do chương trình cung cấp.
- Không được tự bịa tiêu đề, số liệu, nguồn tin hoặc sự kiện.
- Không thêm chi tiết không có trong tiêu đề RSS.
- Dịch title sang tiếng Việt tự nhiên, nhưng không làm thay đổi ý nghĩa.
- Mọi title, summary và tag PHẢI viết bằng tiếng Việt.
- Giữ đúng source_index của tin RSS được chọn.
- Nếu chưa tìm được một số liệu đáng tin cậy, ghi "Chưa có dữ liệu".

Chỉ trả về một JSON object hợp lệ, không dùng Markdown và không thêm
lời giải thích bên ngoài JSON.
"""

TICKERS_BLOCK = """  "tickers": {
    "fed_rate": "giá trị hoặc Chưa có dữ liệu"
  },
"""


def build_system_prompt(sections, include_tickers=False):
    """Dựng prompt chỉ chứa quy tắc và cấu trúc JSON của các mục trong lô.

    Gửi cả 8 mục tốn ~1.205 token chỉ riêng phần prompt; tách theo lô giúp
    mỗi request nằm gọn dưới trần 8.000 token/phút của gói Free.
    """
    rules = "\n".join(
        SECTION_RULES[section] for section in sections if section in SECTION_RULES
    )

    json_parts = []
    for section in sections:
        summary_hint = SECTION_SUMMARY_HINT.get(section, DEFAULT_SUMMARY_HINT)
        json_parts.append(
            '  "%s": [\n'
            '    {\n'
            '      "source_index": 0,\n'
            '      "title": "tiêu đề tiếng Việt",\n'
            '      "summary": "%s",\n'
            '      "tag": "%s"\n'
            '    }\n'
            '  ]' % (section, summary_hint, SECTION_TAG_HINT.get(section, "Bản tin"))
        )

    structure = "{\n"
    if include_tickers:
        structure += TICKERS_BLOCK
    structure += ",\n".join(json_parts)
    structure += "\n}"

    return "%s\nQuy tắc cho các mục trong lượt này:\n%s\n\nCấu trúc bắt buộc:\n\n%s\n" % (
        BASE_RULES,
        rules,
        structure,
    )


# Giữ lại tên cũ cho tương thích; không còn dùng trong luồng chính.
SYSTEM_PROMPT = build_system_prompt(
    ("macro", "vietnam", "ai", "logistics", "gold", "silver", "stocks", "realestate"),
    include_tickers=True,
)


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
    """Thu thập RSS cho cả bản tin chung lẫn kho tin cá nhân hoá.

    Hàm này lấy NHIỀU hơn số bài mà bản tin chung cần. Toàn bộ kết quả đi vào
    pool.json; chỉ phần đầu mỗi mục (theo GROQ_SAMPLE_LIMITS) mới được gửi cho
    Groq qua cap_for_groq().
    """
    sources = {
        "macro": fetch_google_news(
            "(Federal Reserve OR Fed OR US CPI OR US inflation) when:2d",
            "en-US",
            "US",
            "US:en",
            limit=12,
        ),
        "vietnam": fetch_google_news(
            '("VN-Index" OR "USD/VND" OR NHNN OR "FDI Việt Nam") when:2d',
            "vi",
            "VN",
            "VN:vi",
            limit=12,
        ),
        "ai": fetch_google_news(
            "(OpenAI OR Anthropic OR Gemini OR AI model OR AI chip) when:2d",
            "en-US",
            "US",
            "US:en",
            limit=12,
        ),
        "logistics": merge_articles(
            fetch_google_news(
                '("logistics Việt Nam" OR "cảng biển Việt Nam" OR '
                '"chuỗi cung ứng Việt Nam" OR "vận tải hàng hóa" OR '
                '"giá cước vận tải") when:5d',
                "vi",
                "VN",
                "VN:vi",
                limit=8,
            ),
            fetch_google_news(
                '("global logistics" OR "container shipping" OR freight OR '
                '"supply chain disruption" OR "port congestion" OR '
                '"freight rates") when:5d',
                "en-US",
                "US",
                "US:en",
                limit=8,
            ),
            fetch_google_news_optional(
                '("cảng trung chuyển quốc tế Cần Giờ" OR "cảng Cần Giờ" OR '
                '"Cái Mép Thị Vải" OR "cảng Lạch Huyện" OR '
                '"hành lang logistics Việt Nam") when:30d',
                "vi",
                "VN",
                "VN:vi",
                limit=6,
            ),
            fetch_google_news_optional(
                '("Thailand Land Bridge" OR "Kra Canal" OR "Thai Canal" OR '
                '"Southern Economic Corridor Thailand") '
                '(shipping OR port OR logistics OR trade) when:30d',
                "en-US",
                "US",
                "US:en",
                limit=6,
            ),
            fetch_google_news_optional(
                '("Red Sea shipping" OR "Suez Canal" OR "Panama Canal" OR '
                '"Strait of Malacca" OR "South China Sea shipping") when:7d',
                "en-US",
                "US",
                "US:en",
                limit=6,
            ),
            limit=28,
        ),
        "gold": merge_articles(
            fetch_google_news(
                '("giá vàng" OR "vàng SJC" OR "thị trường vàng") when:2d',
                "vi",
                "VN",
                "VN:vi",
                limit=7,
            ),
            fetch_google_news(
                '(gold price OR gold market OR central bank gold) when:2d',
                "en-US",
                "US",
                "US:en",
                limit=6,
            ),
            limit=12,
        ),
        "silver": merge_articles(
            fetch_google_news(
                '("giá bạc" OR "thị trường bạc") when:3d',
                "vi",
                "VN",
                "VN:vi",
                limit=6,
            ),
            fetch_google_news(
                '(silver price OR silver market OR industrial silver) when:3d',
                "en-US",
                "US",
                "US:en",
                limit=7,
            ),
            limit=12,
        ),
        "stocks": fetch_google_news(
            '("VN30" OR "cổ phiếu VN30" OR "thị trường chứng khoán Việt Nam" '
            'OR "VN-Index") when:2d',
            "vi",
            "VN",
            "VN:vi",
            limit=14,
        ),
        "realestate": fetch_google_news(
            '("bất động sản TP.HCM" OR "giá căn hộ TP.HCM" OR '
            '"thị trường nhà ở TP.HCM" OR "pháp lý dự án TP.HCM" OR '
            '"hạ tầng TP.HCM") when:3d',
            "vi",
            "VN",
            "VN:vi",
            limit=14,
        ),
    }

    # Các mục dưới đây CHỈ phục vụ danh mục cá nhân hoá trong pool.json.
    # Dùng fetch_google_news_optional để một truy vấn rỗng không làm hỏng
    # lần chạy của bản tin chung.
    pool_only = {
        "semiconductor": merge_articles(
            fetch_google_news_optional(
                '("bán dẫn" OR "chip bán dẫn" OR "nhà máy chip" OR '
                '"đóng gói kiểm định" OR "vi mạch") when:3d',
                "vi",
                "VN",
                "VN:vi",
                limit=6,
            ),
            fetch_google_news_optional(
                "(semiconductor OR foundry OR wafer OR \"chip fab\" OR "
                '"chip export controls") when:3d',
                "en-US",
                "US",
                "US:en",
                limit=6,
            ),
            limit=10,
        ),
        "energy": merge_articles(
            fetch_google_news_optional(
                '("giá điện" OR "năng lượng tái tạo" OR "điện gió" OR '
                '"điện mặt trời" OR "quy hoạch điện" OR LNG) when:3d',
                "vi",
                "VN",
                "VN:vi",
                limit=6,
            ),
            fetch_google_news_optional(
                '("energy transition" OR "power grid" OR LNG OR '
                '"renewable capacity") when:3d',
                "en-US",
                "US",
                "US:en",
                limit=5,
            ),
            limit=10,
        ),
        "trade_policy": merge_articles(
            fetch_google_news_optional(
                '("thuế quan" OR "hiệp định thương mại" OR "xuất khẩu Việt Nam" '
                'OR "phòng vệ thương mại") when:3d',
                "vi",
                "VN",
                "VN:vi",
                limit=6,
            ),
            fetch_google_news_optional(
                '(tariff OR "trade agreement" OR "export controls" OR '
                '"trade deficit") when:3d',
                "en-US",
                "US",
                "US:en",
                limit=5,
            ),
            limit=10,
        ),
        "fintech_vn": fetch_google_news_optional(
            '("ngân hàng số" OR fintech OR "thanh toán số" OR "ví điện tử" '
            'OR "tín dụng Việt Nam") when:3d',
            "vi",
            "VN",
            "VN:vi",
            limit=10,
        ),
    }

    for section, articles in pool_only.items():
        if articles:
            sources[section] = articles

    for articles in sources.values():
        for source_index, article in enumerate(articles):
            article["source_index"] = source_index

    return sources


def cap_for_groq(news_sources):
    """Cắt danh sách RSS xuống đúng số bài mà Groq được nhận.

    Đây là điểm mấu chốt của P0: pool có thể phình to tuỳ ý mà đầu vào Groq
    không đổi, nên không bao giờ vượt trần 8.000 token/phút của gói Free.

    source_index được gán theo thứ tự trong fetch_news_sources() nên cắt phần
    đầu danh sách vẫn giữ nguyên chỉ số hợp lệ.
    """
    capped = {}
    for section, limit in GROQ_SAMPLE_LIMITS.items():
        capped[section] = list(news_sources.get(section, []))[:limit]
    return capped


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


def validate_tickers(tickers):
    """Kiểm tra riêng phần chỉ số; thiếu chỉ số là lỗi nghiêm trọng."""
    if not isinstance(tickers, dict):
        raise ValueError("Thiếu mục tickers.")

    for ticker in REQUIRED_TICKERS:
        value = tickers.get(ticker)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Ticker {ticker} không hợp lệ.")


def section_is_valid(section, articles):
    """Trả về True nếu mục này dùng được, thay vì ném lỗi giết cả lần chạy.

    Trước 24/09/2026 chỉ cần MỘT mục rỗng là toàn bộ lần chạy thất bại và
    data.json không được cập nhật, kể cả khi 7 mục còn lại hoàn toàn tốt.
    """
    if not isinstance(articles, list) or not articles:
        print(
            f"CẢNH BÁO: mục {section} không có bài viết; sẽ giữ dữ liệu cũ.",
            file=sys.stderr,
            flush=True,
        )
        return False

    for index, article in enumerate(articles, start=1):
        if not isinstance(article, dict):
            print(
                f"CẢNH BÁO: {section}[{index}] không phải object; bỏ mục này.",
                file=sys.stderr,
                flush=True,
            )
            return False

        for field in REQUIRED_NEWS_FIELDS:
            value = article.get(field)
            if not isinstance(value, str) or not value.strip():
                print(
                    f"CẢNH BÁO: {section}[{index}] thiếu trường {field}; "
                    "bỏ mục này.",
                    file=sys.stderr,
                    flush=True,
                )
                return False

    return True


def validate_news_data(data):
    """Bản nghiêm ngặt, giữ lại cho các lệnh gọi cũ và kiểm thử."""
    validate_tickers(data.get("tickers"))

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


def attach_source_metadata(data, news_sources, sections=None):
    """Gắn nguồn, link và thời gian thật từ RSS vào bài Groq đã chọn.

    Bài nào có source_index sai sẽ bị loại bỏ thay vì làm hỏng cả lô, vì
    source_index sai nghĩa là AI bịa chỉ số chứ không phải dữ liệu RSS hỏng.
    """
    for section in (sections or REQUIRED_SECTIONS):
        source_articles = news_sources.get(section, [])
        selected_articles = data.get(section, [])

        if not isinstance(selected_articles, list):
            print(
                f"CẢNH BÁO: mục {section} không phải danh sách; bỏ qua.",
                file=sys.stderr,
                flush=True,
            )
            data[section] = []
            continue

        kept = []
        for position, article in enumerate(selected_articles, start=1):
            if not isinstance(article, dict):
                continue

            source_index = article.pop("source_index", None)
            if not isinstance(source_index, int):
                print(
                    f"CẢNH BÁO: {section}[{position}] thiếu source_index; bỏ bài.",
                    file=sys.stderr,
                    flush=True,
                )
                continue

            if source_index < 0 or source_index >= len(source_articles):
                print(
                    f"CẢNH BÁO: {section}[{position}] có source_index ngoài "
                    "phạm vi; bỏ bài.",
                    file=sys.stderr,
                    flush=True,
                )
                continue

            original = source_articles[source_index]
            article["source"] = original["source"]
            article["url"] = original["url"]
            article["published_at"] = original["published_at"]
            article["id"] = make_article_id(article)
            kept.append(article)

        data[section] = kept


def request_batch_from_groq(
    client, model_name, sections, news_sources, vietnam_time,
    cpi, usd_vnd, vnindex_data, include_tickers,
):
    """Gọi Groq cho ĐÚNG một lô chuyên mục và trả về JSON đã gắn nguồn."""
    compact_sources = {
        section: [
            {
                "source_index": article["source_index"],
                "title": article["title"],
                "source": article["source"],
                "published_at": article["published_at"],
            }
            for article in news_sources.get(section, [])
        ]
        for section in sections
    }

    ticker_note = ""
    if include_tickers:
        ticker_note = (
            "\nCác số liệu dưới đây đã lấy từ API, KHÔNG được thay đổi:\n"
            f"- CPI Mỹ theo năm: {cpi}\n"
            f"- Tỷ giá tham khảo USD/VND: {usd_vnd}\n"
            f"- VN-Index: {vnindex_data['vnindex']}\n"
            "\nVới fed_rate: chỉ điền nếu có nguồn đáng tin cậy, nếu không "
            'ghi "Chưa có dữ liệu".\n'
        )

    user_prompt = f"""
Thời gian hiện tại tại Việt Nam: {vietnam_time}.

Hãy biên tập bản tin từ đúng danh sách tiêu đề RSS dưới đây.
Không tự tìm thêm hoặc thêm chi tiết không có trong tiêu đề.
{ticker_note}
Danh sách tiêu đề RSS:
{json.dumps(compact_sources, ensure_ascii=False)}

Nhắc lại: mọi title, summary và tag bắt buộc hoàn toàn bằng tiếng Việt.
Giữ nguyên source_index của từng tin được chọn.
Trả về đúng JSON theo cấu trúc được yêu cầu.
"""

    response = client.chat.completions.create(
        model=model_name,
        messages=[
            {
                "role": "system",
                "content": build_system_prompt(sections, include_tickers),
            },
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.1,
        max_completion_tokens=MAX_COMPLETION_TOKENS_PER_BATCH,
        response_format={"type": "json_object"},
    )

    content = response.choices[0].message.content
    if not content:
        raise ValueError("Groq trả về nội dung rỗng.")

    batch_data = extract_json(content)
    attach_source_metadata(batch_data, news_sources, sections)
    return batch_data


def fetch_news_from_groq(
    client, cpi, usd_vnd, vnindex_data, news_sources, previous_data=None,
):
    """Biên tập bản tin theo từng lô để không vượt trần 8.000 token/phút.

    Mỗi lô thất bại chỉ làm mất các mục của lô đó; các mục còn lại vẫn được
    cập nhật, và mục thiếu sẽ lấy lại nội dung cũ kèm thời gian cũ.
    """
    vietnam_time = datetime.now(
        ZoneInfo("Asia/Ho_Chi_Minh")
    ).strftime("%d/%m/%Y %H:%M")

    previous_data = previous_data or {}
    data = {"tickers": {}}
    good_sections = []
    stale_sections = []
    errors = []

    for batch_index, sections in enumerate(SECTION_BATCHES):
        include_tickers = batch_index == 0

        if batch_index > 0:
            print(
                f"Nghỉ {SLEEP_BETWEEN_BATCHES} giây để sang cửa sổ giới hạn "
                "token mới...",
                flush=True,
            )
            time.sleep(SLEEP_BETWEEN_BATCHES)

        batch_done = False

        for model_name in MODELS_TO_TRY:
            if batch_done:
                break

            for attempt in range(1, 3):
                label = "+".join(sections)
                print(
                    f"Lô {batch_index + 1}/{len(SECTION_BATCHES)} ({label}) "
                    f"— model {model_name}, lần {attempt}/2...",
                    flush=True,
                )

                try:
                    batch_data = request_batch_from_groq(
                        client, model_name, sections, news_sources,
                        vietnam_time, cpi, usd_vnd, vnindex_data,
                        include_tickers,
                    )

                    if include_tickers:
                        fed_rate = (batch_data.get("tickers") or {}).get(
                            "fed_rate"
                        )
                        data["tickers"]["fed_rate"] = (
                            fed_rate
                            if isinstance(fed_rate, str) and fed_rate.strip()
                            else "Chưa có dữ liệu"
                        )

                    for section in sections:
                        articles = batch_data.get(section)
                        if section_is_valid(section, articles):
                            data[section] = articles
                            good_sections.append(section)

                    batch_done = True
                    break

                except Exception as error:
                    detail = f"{type(error).__name__}: {error}"
                    is_rate_limit = "429" in detail or "RateLimit" in detail

                    if is_rate_limit and attempt < 2:
                        print(
                            f"Groq giới hạn token; chờ {SLEEP_BETWEEN_BATCHES} "
                            "giây rồi thử lại...",
                            file=sys.stderr,
                            flush=True,
                        )
                        time.sleep(SLEEP_BETWEEN_BATCHES)
                        continue

                    errors.append(f"lô {label} / {model_name}: {detail}")
                    print(
                        f"Lô {label} với {model_name} thất bại: {detail}",
                        file=sys.stderr,
                        flush=True,
                    )
                    break

    # Mục nào không lấy được thì giữ nguyên nội dung cũ thay vì bỏ trống.
    for section in REQUIRED_SECTIONS:
        if section in data:
            continue

        old_articles = previous_data.get(section)
        if isinstance(old_articles, list) and old_articles:
            data[section] = old_articles
            stale_sections.append(section)
        else:
            data[section] = []

    if len(good_sections) * 2 < len(REQUIRED_SECTIONS):
        raise RuntimeError(
            "Quá nửa số chuyên mục thất bại nên không ghi đè data.json:\n- "
            + "\n- ".join(errors or ["không rõ nguyên nhân"])
        )

    data["tickers"].setdefault("fed_rate", "Chưa có dữ liệu")
    data["tickers"]["cpi"] = cpi
    data["tickers"]["usd_vnd"] = usd_vnd
    data["tickers"].update(vnindex_data)
    data["updated_at"] = vietnam_time

    validate_tickers(data["tickers"])

    if stale_sections:
        data["stale_sections"] = stale_sections
        print(
            "CẢNH BÁO: giữ dữ liệu cũ cho các mục: "
            + ", ".join(stale_sections),
            file=sys.stderr,
            flush=True,
        )

    print(
        f"Đã cập nhật {len(good_sections)}/{len(REQUIRED_SECTIONS)} chuyên mục "
        f"mới: {', '.join(good_sections)}",
        flush=True,
    )

    if errors:
        print(
            "Một số lô gặp lỗi nhưng lần chạy vẫn tiếp tục:\n- "
            + "\n- ".join(errors),
            file=sys.stderr,
            flush=True,
        )

    return data


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
                max_completion_tokens=4000,
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


def build_pool(news_sources, edited_data=None):
    """Chuyển RSS thô thành danh sách bài cho pool.json.

    Bài nào đã được Groq biên tập sẽ có title_vi/summary và edited=True;
    phần còn lại chỉ có tiêu đề gốc từ RSS (edited=False) — giao diện phải
    nói rõ giới hạn này cho người dùng.
    """
    now_iso = datetime.now(
        ZoneInfo("Asia/Ho_Chi_Minh")
    ).isoformat(timespec="seconds")

    edited_by_url = {}
    for section in REQUIRED_SECTIONS:
        for article in (edited_data or {}).get(section) or []:
            if isinstance(article, dict) and article.get("url"):
                edited_by_url[article["url"]] = article

    articles = []
    for section, raw_articles in news_sources.items():
        for raw in raw_articles:
            if not isinstance(raw, dict) or not raw.get("title"):
                continue

            url = raw.get("url", "")
            edited = edited_by_url.get(url) or {}
            article = {
                "id": make_article_id(
                    {
                        "url": url,
                        "source": raw.get("source", ""),
                        "title": raw.get("title", ""),
                    }
                ),
                "title": raw.get("title", ""),
                "title_vi": edited.get("title", ""),
                "summary": edited.get("summary", ""),
                "tag": edited.get("tag", ""),
                "source": raw.get("source", ""),
                "url": url,
                "published_at": raw.get("published_at", ""),
                "section": section,
                "first_seen_at": now_iso,
                "edited": bool(edited),
            }
            articles.append(article)

    return articles


def update_pool(new_articles):
    """Gộp bài mới vào pool.json, loại trùng theo id và giữ 14 ngày."""
    now = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh"))
    pool_data = load_json_file(POOL_FILE, {"articles": []})

    merged = {}
    for article in list(pool_data.get("articles", [])) + list(new_articles):
        if not isinstance(article, dict) or not article.get("title"):
            continue

        article = dict(article)
        article["id"] = article.get("id") or make_article_id(article)
        existing = merged.get(article["id"])

        if existing:
            # Giữ thời điểm nhìn thấy lần đầu và ưu tiên bản đã biên tập.
            article["first_seen_at"] = (
                existing.get("first_seen_at") or article.get("first_seen_at")
            )
            if existing.get("edited") and not article.get("edited"):
                article = existing

        merged[article["id"]] = article

    cutoff = now - timedelta(days=POOL_RETENTION_DAYS)
    retained = [
        article
        for article in merged.values()
        if parse_article_time(article) >= cutoff
    ]
    retained.sort(key=parse_article_time, reverse=True)

    edited_count = sum(1 for article in retained if article.get("edited"))

    return {
        "updated_at": now.strftime("%d/%m/%Y %H:%M"),
        "retention_days": POOL_RETENTION_DAYS,
        "article_count": len(retained),
        "edited_count": edited_count,
        "sections": sorted({article.get("section", "") for article in retained}),
        "disclaimer": (
            "Bài có edited=false chỉ gồm tiêu đề gốc từ RSS, chưa được biên "
            "tập sang tiếng Việt. Luôn đọc bài gốc theo url."
        ),
        "articles": retained,
    }


def save_pool(news_sources, edited_data=None):
    """Ghi pool.json và không bao giờ để lỗi pool làm hỏng lần chạy chính."""
    try:
        pool = update_pool(build_pool(news_sources, edited_data))
        write_json_atomically(pool, POOL_FILE)
        print(
            f"Đã ghi pool.json: {pool['article_count']} bài "
            f"({pool['edited_count']} bài đã biên tập).",
            flush=True,
        )
        return pool
    except Exception as pool_error:
        print(
            f"CẢNH BÁO: không ghi được pool.json: "
            f"{type(pool_error).__name__}: {pool_error}",
            file=sys.stderr,
            flush=True,
        )
        return None


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

        # Ghi pool NGAY từ RSS thô, trước khi gọi Groq. Nhờ vậy kho tin vẫn
        # lớn lên kể cả khi AI thất bại hoàn toàn.
        save_pool(news_sources)

        # Groq chỉ nhận phần mẫu đã cắt; phần dư của pool không tốn token.
        groq_sources = cap_for_groq(news_sources)
        print(
            "Mẫu gửi Groq: "
            + ", ".join(
                f"{section}={len(items)}"
                for section, items in groq_sources.items()
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
            groq_sources,
            previous_data,
        )
        data["market_snapshot"] = market_snapshot
        data["real_estate_market"] = fetch_real_estate_market(previous_data)
        data["investment_overview"] = build_investment_overview(data)
        history = update_history(data, previous_data)

        try:
            print(
                f"Nghỉ {SLEEP_BETWEEN_BATCHES} giây trước khi phân tích "
                "xu hướng để không cộng dồn token cùng một phút...",
                flush=True,
            )
            time.sleep(SLEEP_BETWEEN_BATCHES)
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

        # Ghi lại pool để đánh dấu những bài đã được Groq biên tập.
        save_pool(news_sources, data)

        print(
            "Đã cập nhật data.json, history.json và pool.json thành công.",
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
