// Edge Function: chuyển hội thoại của người dùng thành bộ quy tắc lọc tin.
//
// Vì sao phải là Edge Function chứ không gọi Groq thẳng từ trình duyệt:
// index.html là file công khai trên GitHub Pages. Nhúng GROQ_API_KEY vào đó là
// lộ key cho toàn internet. Key chỉ được nằm trong Supabase secret.
//
// Hàm này KHÔNG lưu gì vào cơ sở dữ liệu. Nó trả spec về cho trình duyệt kèm
// kết quả chạy thử trên pool.json; người dùng xem trước rồi mới bấm lưu.

import { createClient } from "jsr:@supabase/supabase-js@2";

const GROQ_URL = "https://api.groq.com/openai/v1/chat/completions";
const GROQ_MODELS = ["openai/gpt-oss-120b", "openai/gpt-oss-20b"];
const DAILY_LIMIT = 20;

const POOL_URL =
  "https://khoadinh-lab.github.io/My-daily-finance-and-AI-news/pool.json";

const ALLOWED_ORIGINS = new Set([
  "https://khoadinh-lab.github.io",
  "http://localhost:5173",
  "http://127.0.0.1:5500",
]);

const VALID_SECTIONS = new Set([
  "macro", "vietnam", "ai", "logistics", "gold", "silver", "stocks",
  "realestate", "semiconductor", "energy", "trade_policy", "fintech_vn",
]);

// Những từ khoá khớp gần như mọi bài, làm danh mục trở nên vô dụng.
const BANNED_KEYWORDS = new Set([
  "tin tuc", "tin tức", "thi truong", "thị trường", "kinh te", "kinh tế",
  "news", "market", "hom nay", "hôm nay", "viet nam", "việt nam", "the gioi",
  "thế giới", "bao", "báo", "bai viet", "bài viết",
]);

const SYSTEM_PROMPT = `Bạn là bộ chuyển đổi yêu cầu người dùng thành bộ lọc tin tức.

NHIỆM VỤ: đọc hội thoại và sinh ra MỘT JSON object đúng cấu trúc được yêu cầu.

QUY TẮC BẮT BUỘC:
- Chỉ trả về JSON, không Markdown, không giải thích.
- include_keywords: 3-20 từ khoá. PHẢI gồm cả biến thể tiếng Việt VÀ tiếng Anh
  của cùng một khái niệm, vì kho tin có cả hai ngôn ngữ.
  Ví dụ: "bán dẫn" thì phải kèm "semiconductor" và "chip".
- Từ khoá là DANH TỪ hoặc CỤM DANH TỪ cụ thể. Tuyệt đối không dùng từ chung
  chung như "tin tức", "thị trường", "kinh tế", "Việt Nam", "hôm nay" — chúng
  khớp mọi thứ và làm danh mục vô dụng.
- boost_keywords: yếu tố người dùng muốn ƯU TIÊN chứ không bắt buộc, thường là
  địa danh. Tối đa 10.
- exclude_keywords: chỉ điền khi người dùng nói rõ điều họ KHÔNG muốn.
- base_categories: chỉ điền khi người dùng nói rõ chuyên mục. Mặc định null.
- name: tên danh mục ngắn gọn bằng tiếng Việt, tối đa 60 ký tự.
- Nếu yêu cầu còn quá mơ hồ, đặt needs_clarification là mảng 1-2 câu hỏi ngắn
  bằng tiếng Việt và để include_keywords là mảng rỗng.

KHÔNG được bịa tên nguồn tin. KHÔNG hứa hẹn số lượng tin.

Cấu trúc bắt buộc:
{
  "name": "tên danh mục",
  "include_keywords": ["..."],
  "boost_keywords": ["..."],
  "exclude_keywords": ["..."],
  "base_categories": null,
  "max_items_per_day": 12,
  "needs_clarification": []
}`;

// ------------------------------------------------------------------ tiện ích

function corsHeaders(origin: string | null) {
  const allow = origin && ALLOWED_ORIGINS.has(origin) ? origin : "";
  return {
    "Access-Control-Allow-Origin": allow,
    "Access-Control-Allow-Headers": "authorization, content-type",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Vary": "Origin",
  };
}

function json(body: unknown, status: number, origin: string | null) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...corsHeaders(origin) },
  });
}

function stripAccents(text: string) {
  return text.normalize("NFD").replace(/[̀-ͯ]/g, "")
    .replace(/đ/g, "d").replace(/Đ/g, "D");
}

function normalize(text: string) {
  return stripAccents(String(text ?? "")).toLowerCase()
    .replace(/[^a-z0-9\s]+/g, " ").replace(/\s+/g, " ").trim();
}

function hasPhrase(haystack: string, keyword: string) {
  const k = normalize(keyword);
  if (!k) return false;
  const re = new RegExp(
    "(?:^|[^a-z0-9])" + k.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "(?:[^a-z0-9]|$)",
  );
  return re.test(haystack);
}

function cleanKeywords(value: unknown, max: number) {
  if (!Array.isArray(value)) return [];
  const seen = new Set<string>();
  const out: string[] = [];
  for (const raw of value) {
    if (typeof raw !== "string") continue;
    const word = raw.trim();
    if (word.length < 2 || word.length > 40) continue;
    const key = normalize(word);
    if (!key || seen.has(key) || BANNED_KEYWORDS.has(key)) continue;
    seen.add(key);
    out.push(word);
    if (out.length >= max) break;
  }
  return out;
}

/** Ép phản hồi của AI về đúng khuôn; không bao giờ tin thẳng đầu ra của model. */
function sanitizeSpec(raw: Record<string, unknown>) {
  const include = cleanKeywords(raw.include_keywords, 20);
  const clarify = Array.isArray(raw.needs_clarification)
    ? raw.needs_clarification.filter((q) => typeof q === "string" && q.trim())
      .slice(0, 2).map((q) => String(q).trim().slice(0, 200))
    : [];

  if (include.length < 3) {
    return {
      ok: false as const,
      needs_clarification: clarify.length ? clarify : [
        "Bạn muốn theo dõi chủ đề gì cụ thể hơn? Ví dụ một ngành, một thị trường hoặc một loại sự kiện.",
      ],
    };
  }

  let base: string[] | null = null;
  if (Array.isArray(raw.base_categories)) {
    const picked = raw.base_categories
      .filter((s): s is string => typeof s === "string" && VALID_SECTIONS.has(s));
    base = picked.length ? picked : null;
  }

  const maxItems = Number(raw.max_items_per_day);
  const name = typeof raw.name === "string" && raw.name.trim()
    ? raw.name.trim().slice(0, 60)
    : "Danh mục của tôi";

  return {
    ok: true as const,
    name,
    spec: {
      version: 1,
      include_keywords: include,
      boost_keywords: cleanKeywords(raw.boost_keywords, 10),
      exclude_keywords: cleanKeywords(raw.exclude_keywords, 15),
      base_categories: base,
      max_items_per_day: Number.isFinite(maxItems)
        ? Math.min(30, Math.max(5, Math.round(maxItems)))
        : 12,
      weights: { match: 0.5, fresh: 0.25, trust: 0.15, boost: 0.15 },
      half_life_hours: 24,
    },
  };
}

/** Chạy thử spec trên pool.json để người dùng thấy trước khi lưu. */
async function previewAgainstPool(spec: Record<string, unknown>) {
  const response = await fetch(POOL_URL, { cache: "no-store" });
  if (!response.ok) {
    return { available: false, total: 0, fresh48: 0, samples: [] as unknown[] };
  }

  const pool = await response.json();
  const articles = Array.isArray(pool.articles) ? pool.articles : [];
  const now = Date.now();
  const include = spec.include_keywords as string[];
  const exclude = (spec.exclude_keywords as string[]) ?? [];
  const base = spec.base_categories as string[] | null;

  let total = 0;
  let fresh48 = 0;
  const samples: unknown[] = [];

  for (const article of articles) {
    const hay = normalize(
      `${article.title_vi || article.title || ""} ${article.summary || ""} ${article.tag || ""}`,
    );
    if (exclude.some((k) => hasPhrase(hay, k))) continue;
    if (base && base.length) {
      const sections: string[] = Array.isArray(article.sections) && article.sections.length
        ? article.sections
        : [article.section];
      if (!sections.some((s) => base.includes(s))) continue;
    }
    if (!include.some((k) => hasPhrase(hay, k))) continue;

    total++;
    const stamp = Date.parse(article.published_at || article.first_seen_at || "");
    if (!Number.isNaN(stamp) && (now - stamp) / 3600000 <= 48) fresh48++;
    if (samples.length < 3) {
      samples.push({
        title: article.title_vi || article.title || "",
        source: article.source || "",
        url: article.url || "",
        edited: Boolean(article.edited),
      });
    }
  }

  return { available: true, total, fresh48, samples, pool_size: articles.length };
}

async function callGroq(apiKey: string, messages: unknown[]) {
  const errors: string[] = [];

  for (const model of GROQ_MODELS) {
    try {
      const response = await fetch(GROQ_URL, {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${apiKey}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          model,
          messages: [{ role: "system", content: SYSTEM_PROMPT }, ...messages],
          temperature: 0.2,
          max_completion_tokens: 1200,
          response_format: { type: "json_object" },
        }),
      });

      if (!response.ok) {
        errors.push(`${model}: HTTP ${response.status}`);
        continue;
      }

      const payload = await response.json();
      const content = payload?.choices?.[0]?.message?.content;
      if (!content) {
        errors.push(`${model}: phản hồi rỗng`);
        continue;
      }
      return JSON.parse(content);
    } catch (error) {
      errors.push(`${model}: ${error instanceof Error ? error.message : error}`);
    }
  }

  throw new Error(errors.join(" | "));
}

// -------------------------------------------------------------------- handler

Deno.serve(async (request) => {
  const origin = request.headers.get("origin");

  if (request.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: corsHeaders(origin) });
  }
  if (request.method !== "POST") {
    return json({ error: "method_not_allowed" }, 405, origin);
  }
  if (origin && !ALLOWED_ORIGINS.has(origin)) {
    return json({ error: "origin_not_allowed" }, 403, origin);
  }

  const authHeader = request.headers.get("Authorization") ?? "";
  if (!authHeader.startsWith("Bearer ")) {
    return json({ error: "missing_token" }, 401, origin);
  }

  // Dùng chính JWT của người dùng: RLS được áp dụng, và user_id lấy từ token
  // đã xác thực chứ KHÔNG bao giờ lấy từ body request.
  const supabase = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_ANON_KEY")!,
    { global: { headers: { Authorization: authHeader } } },
  );

  const { data: userData, error: userError } = await supabase.auth.getUser();
  if (userError || !userData?.user) {
    return json({ error: "invalid_token" }, 401, origin);
  }
  const userId = userData.user.id;

  // Chặn đốt quota Groq trước khi gọi model.
  const since = new Date(Date.now() - 24 * 3600 * 1000).toISOString();
  const { count, error: countError } = await supabase
    .from("feed_events")
    .select("id", { count: "exact", head: true })
    .eq("user_id", userId)
    .eq("event_name", "feed_compose")
    .gte("occurred_at", since);

  if (countError) {
    return json({ error: "rate_check_failed" }, 500, origin);
  }
  if ((count ?? 0) >= DAILY_LIMIT) {
    return json({
      error: "rate_limited",
      message: `Bạn đã tạo/sửa danh mục ${DAILY_LIMIT} lần trong 24 giờ qua. Hãy thử lại sau.`,
    }, 429, origin);
  }

  let body: { messages?: unknown };
  try {
    body = await request.json();
  } catch {
    return json({ error: "invalid_body" }, 400, origin);
  }

  const messages = Array.isArray(body.messages) ? body.messages : [];
  const clean = messages
    .filter((m): m is { role: string; content: string } =>
      !!m && typeof m === "object" &&
      typeof (m as Record<string, unknown>).role === "string" &&
      typeof (m as Record<string, unknown>).content === "string"
    )
    .filter((m) => m.role === "user" || m.role === "assistant")
    .slice(-8)
    .map((m) => ({ role: m.role, content: m.content.slice(0, 2000) }));

  if (!clean.length) {
    return json({ error: "empty_conversation" }, 400, origin);
  }

  await supabase.from("feed_events").insert({
    user_id: userId,
    event_name: "feed_compose",
  });

  let raw: Record<string, unknown>;
  try {
    raw = await callGroq(Deno.env.get("GROQ_API_KEY")!, clean);
  } catch (error) {
    console.error("groq_failed", error instanceof Error ? error.message : error);
    return json({
      error: "ai_unavailable",
      message: "Hiện chưa tạo được danh mục. Vui lòng thử lại sau ít phút.",
    }, 502, origin);
  }

  const result = sanitizeSpec(raw);
  if (!result.ok) {
    return json({
      status: "needs_clarification",
      questions: result.needs_clarification,
    }, 200, origin);
  }

  let preview;
  try {
    preview = await previewAgainstPool(result.spec);
  } catch {
    preview = { available: false, total: 0, fresh48: 0, samples: [] };
  }

  return json({
    status: "ok",
    name: result.name,
    spec: result.spec,
    preview,
  }, 200, origin);
});
