#!/usr/bin/env python3
"""AI / Agent 每日早报 —— 生成四板块各 10 条，邮件发送。

板块：
  1) AI 热点（10 条新闻）
  2) GitHub 热门 AI 项目（10 个仓库）
  3) Agent 热点（10 条新闻）
  4) GitHub 热门 Agent 项目（10 个仓库）

数据源全部免费：RSS/Atom 订阅源 + Hacker News Algolia API + GitHub 公开 Search API。
摘要由 DeepSeek chat API 生成；任何一步失败都有兜底，保证每天照发 40 条。

环境变量：
  DEEPSEEK_API_KEY   必填（生成中文摘要）
  MAIL_USERNAME      SMTP 登录用户名（省略则只生成不发信）
  MAIL_APP_PASSWORD  SMTP 登录密码/密钥
  MAIL_TO            收件地址（必填）
  MAIL_FROM          发件人地址（默认同 MAIL_USERNAME）；Brevo 场景下填已验证的发件地址
  SMTP_HOST          默认 smtp.gmail.com；Brevo=smtp-relay.brevo.com，Resend=smtp.resend.com
  SMTP_PORT          默认 465（SSL）；587 走 STARTTLS
  GH_TOKEN           GitHub token（CI 里用 secrets.GITHUB_TOKEN 提升限流额度）
  DRY_RUN=1          不发信，写 preview.html / preview.txt 供预览
"""

from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from pathlib import Path
from xml.etree import ElementTree as ET

UA = "Mozilla/5.0 (compatible; ai-digest-bot/1.0)"
ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / ".state" / "seen.json"
SEEN_DAYS = 7
PER_SECTION = 10
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in {"1", "true", "yes"}
MAIL_TO = os.environ.get("MAIL_TO", "").strip()
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash").strip()
DEEPSEEK_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/") + "/chat/completions"

NOW = dt.datetime.now(dt.timezone.utc)

# ---------------------------------------------------------------- 订阅源
# (名称, 地址, 是否偏 agent 话题)
FEEDS: list[tuple[str, str, bool]] = [
    ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/", False),
    ("VentureBeat AI", "https://venturebeat.com/category/ai/feed/", False),
    ("The Verge AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", False),
    ("Ars Technica AI", "https://arstechnica.com/ai/feed/", False),
    ("MIT Tech Review", "https://www.technologyreview.com/topic/artificial-intelligence/feed", False),
    ("Google AI Blog", "https://blog.google/technology/ai/rss/", False),
    ("OpenAI News", "https://openai.com/news/rss.xml", False),
    ("Hugging Face Blog", "https://huggingface.co/blog/feed.xml", False),
    ("DeepMind Blog", "https://deepmind.google/blog/rss.xml", False),
    ("NVIDIA Blog", "https://blogs.nvidia.com/feed/", False),
    ("The Decoder", "https://the-decoder.com/feed/", False),
    ("Simon Willison", "https://simonwillison.net/atom/everything/", True),
    ("Hacker News", "https://news.ycombinator.com/rss", False),
]

AGENT_WORDS = (
    "agent", "agentic", "multi-agent", "mcp", "model context protocol", "tool use",
    "tool-use", "autonomous", "computer use", "coding agent", "claude code", "codex",
    "copilot", "智能体", "agent 框架", "orchestration", "function calling",
)
AI_WORDS = (
    "ai", "llm", "gpt", "claude", "gemini", "model", "openai", "anthropic", "deepseek",
    "qwen", "llama", "mistral", "neural", "inference", "training", "transformer",
    "diffusion", "rag", "fine-tun", "benchmark", "artificial intelligence", "大模型",
)

# ---------------------------------------------------------------- 小工具


def log(msg: str) -> None:
    print(f"[digest] {msg}", flush=True)


def http_get(url: str, *, accept: str = "*/*", timeout: int = 25) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def get_json(url: str, *, headers: dict[str, str] | None = None, timeout: int = 25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def strip_html(text: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text or "", flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def load_seen() -> set[str]:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return set()
    cutoff = (NOW - dt.timedelta(days=SEEN_DAYS)).timestamp()
    return {u for u, ts in (data.get("urls") or {}).items() if isinstance(ts, (int, float)) and ts >= cutoff}


def save_seen(urls: list[str], previous: dict | None = None) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {"urls": {}, "updated": NOW.isoformat()}
    cutoff = (NOW - dt.timedelta(days=SEEN_DAYS)).timestamp()
    for u, ts in ((previous or {}).get("urls") or {}).items():
        if isinstance(ts, (int, float)) and ts >= cutoff:
            data["urls"][u] = ts
    for u in urls:
        data["urls"][u] = NOW.timestamp()
    STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"记录 {len(data['urls'])} 条已推链接到 {STATE_FILE}")


def read_state_raw() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ---------------------------------------------------------------- 采集：新闻


def parse_feed(xml: bytes, source: str) -> list[dict]:
    """同时兼容 RSS 2.0 和 Atom。"""
    out: list[dict] = []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return out
    ns = {"atom": "http://www.w3.org/2005/Atom"}

    def pick(node, *names):
        for n in names:
            el = node.find(n)
            if el is not None and (el.text or "").strip():
                return el
            el = node.find(f"atom:{n}", ns)
            if el is not None and (el.text or "").strip():
                return el
        return None

    for item in list(root.iter("item")) + list(root.iter("{http://www.w3.org/2005/Atom}entry")):
        title_el = pick(item, "title")
        if title_el is None:
            continue
        link = ""
        link_el = pick(item, "link")
        if link_el is not None:
            link = (link_el.text or "").strip() or link_el.get("href", "")
        if not link:
            for cand in item.findall("{http://www.w3.org/2005/Atom}link"):
                if cand.get("rel") in (None, "alternate") and cand.get("href"):
                    link = cand.get("href")
                    break
        date_el = pick(item, "pubDate", "published", "updated", "date")
        published = None
        if date_el is not None:
            published = parse_date((date_el.text or "").strip())
        body_el = pick(item, "description", "summary", "content", "encoded")
        out.append({
            "title": strip_html(title_el.text or ""),
            "url": link,
            "source": source,
            "published": published,
            "text": strip_html(body_el.text if body_el is not None and body_el.text else "")[:400],
        })
    return out


def parse_date(value: str) -> dt.datetime | None:
    from email.utils import parsedate_to_datetime

    if not value:
        return None
    try:
        d = parsedate_to_datetime(value)
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except Exception:
        pass
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})", value)
    if m:
        y, mo, d_, h, mi, s = map(int, m.groups())
        return dt.datetime(y, mo, d_, h, mi, s, tzinfo=dt.timezone.utc)
    return None


def collect_feed_news() -> list[dict]:
    items: list[dict] = []
    for name, url, _agentish in FEEDS:
        try:
            xml = http_get(url, accept="application/rss+xml, application/atom+xml, application/xml, text/xml")
            got = parse_feed(xml, name)
            log(f"  {name}: {len(got)} 条")
            items.extend(got)
        except Exception as exc:  # noqa: BLE001
            log(f"  {name}: 失败 ({type(exc).__name__}: {exc})")
    return items


HN_QUERIES = [
    ("AI", "https://hn.algolia.com/api/v1/search_by_date?tags=story&hitsPerPage=80&numericFilters=points%3E60&query=AI"),
    ("LLM", "https://hn.algolia.com/api/v1/search_by_date?tags=story&hitsPerPage=80&numericFilters=points%3E60&query=LLM"),
    ("agent", "https://hn.algolia.com/api/v1/search_by_date?tags=story&hitsPerPage=80&numericFilters=points%3E30&query=agent"),
    ("MCP", "https://hn.algolia.com/api/v1/search_by_date?tags=story&hitsPerPage=50&numericFilters=points%3E20&query=MCP"),
]


def collect_hn_news() -> list[dict]:
    items: list[dict] = []
    for label, url in HN_QUERIES:
        try:
            data = get_json(url)
            hits = data.get("hits") or []
            log(f"  HN/{label}: {len(hits)} 条")
            for h in hits:
                title = (h.get("title") or "").strip()
                link = h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}"
                if not title:
                    continue
                items.append({
                    "title": title,
                    "url": link,
                    "source": f"Hacker News ({h.get('points')} 赞)",
                    "published": dt.datetime.fromtimestamp(h.get("created_at_i") or 0, dt.timezone.utc),
                    "text": strip_html(h.get("story_text") or "")[:300],
                })
        except Exception as exc:  # noqa: BLE001
            log(f"  HN/{label}: 失败 ({type(exc).__name__}: {exc})")
    return items


def dedupe_news(items: list[dict]) -> list[dict]:
    seen_title: set[str] = set()
    out: list[dict] = []
    for it in sorted(items, key=lambda x: x["published"] or dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc), reverse=True):
        key = re.sub(r"[^a-z0-9]+", "", (it["title"] or "").lower())[:60]
        if not key or key in seen_title or not it.get("url"):
            continue
        if it["published"] and (NOW - it["published"]) > dt.timedelta(days=4):
            continue
        seen_title.add(key)
        out.append(it)
    return out


def _keyword_hit(blob: str, words: tuple[str, ...]) -> bool:
    """短英文词按词边界匹配（否则 "ai" 会命中 said/email/domain），中文/短语用子串。"""
    for w in words:
        w = w.strip()
        if not w:
            continue
        if w.isascii() and len(w) <= 5 and w.replace("-", "").isalnum():
            if re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", blob):
                return True
        elif w in blob:
            return True
    return False


def filter_news(items: list[dict], kind: str) -> list[dict]:
    words = AGENT_WORDS if kind == "agent" else AI_WORDS
    picked = []
    for it in items:
        blob = f"{it['title']} {it.get('text','')}".lower()
        if _keyword_hit(blob, words):
            picked.append(it)
    return picked


# ---------------------------------------------------------------- 采集：GitHub


def gh_search(query: str, per_page: int = 50) -> list[dict]:
    url = ("https://api.github.com/search/repositories?q=" + urllib.parse.quote(query)
           + f"&sort=stars&order=desc&per_page={per_page}")
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = get_json(url, headers=headers)
    return data.get("items") or []


def collect_repos(kind: str) -> list[dict]:
    since_active = (NOW - dt.timedelta(days=3)).strftime("%Y-%m-%d")
    since_new = (NOW - dt.timedelta(days=45)).strftime("%Y-%m-%d")
    if kind == "agent":
        queries = [
            f"agent in:name,description,topics stars:>150 pushed:>{since_active}",
            f"mcp in:name,description,topics stars:>80 pushed:>{since_active}",
            f"agentic OR \"multi-agent\" in:name,description stars:>100 created:>{since_new}",
        ]
    else:
        queries = [
            f"ai OR llm in:name,description,topics stars:>500 pushed:>{since_active}",
            f"llm in:name,description,topics stars:>300 created:>{since_new}",
            f"machine-learning OR genai in:name,description stars:>300 pushed:>{since_active}",
            f"inference OR diffusion OR rag in:name,description,topics stars:>200 pushed:>{since_active}",
        ]
    out: dict[str, dict] = {}
    for q in queries:
        try:
            items = gh_search(q)
            log(f"  GitHub[{kind}] {q[:46]}... → {len(items)} 个")
            for r in items:
                full = r.get("full_name")
                if not full or r.get("fork"):
                    continue
                created = (r.get("created_at") or "")[:10]
                try:
                    age_days = max((NOW - dt.datetime.strptime(created, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)).days, 1)
                except Exception:  # noqa: BLE001
                    age_days = 365
                stars = r.get("stargazers_count") or 0
                desc = strip_html(r.get("description") or "")[:240]
                topics = ", ".join((r.get("topics") or [])[:6])
                out[full] = {
                    "name": full,
                    "url": r.get("html_url"),
                    "stars": stars,
                    "desc": desc,
                    "language": r.get("language") or "",
                    "created": created,
                    "pushed": (r.get("pushed_at") or "")[:10],
                    "topics": topics,
                    "agentish": _agentish(full, desc, topics),
                    "rising": stars / age_days,
                }
        except Exception as exc:  # noqa: BLE001
            log(f"  GitHub[{kind}] 查询失败 ({type(exc).__name__}: {exc})")
        time.sleep(1.5)
    return list(out.values())


REPO_AGENT_WORDS = (
    "agent", "agentic", "multi-agent", "mcp", "tool-use", "tool use", "autonomous",
    "copilot", "claude-code", "claude code", "codex", "coding assistant", "subagent",
    "orchestrat", "browser-use", "computer-use", "agent framework", "智能体",
)


def _agentish(name: str, desc: str, topics: str) -> bool:
    blob = f"{name} {desc} {topics}".lower()
    return any(w in blob for w in REPO_AGENT_WORDS)


# ---------------------------------------------------------------- LLM 选编


SYSTEM_PROMPTS = {
    "news_ai": (
        "你是资深 AI 行业主编，为中文读者挑选每日要闻。只输出 JSON，不要任何解释。"
    ),
    "news_agent": (
        "你是资深 AI Agent（智能体）领域主编，关注 agent 框架、MCP、工具调用、自主执行、编码智能体、多智能体协作、agent 安全与治理。只输出 JSON。"
    ),
    "repos_ai": (
        "你是资深开源观察者，为中文读者挑选值得关注的 AI 开源项目。只输出 JSON，不要任何解释。"
    ),
    "repos_agent": (
        "你是资深开源观察者，专挑 AI Agent / MCP / 智能体工具链相关的开源项目。只输出 JSON，不要任何解释。"
    ),
}


def llm_json(api_key: str, system: str, user: str, *, max_tokens: int = 4000) -> dict | None:
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0.3,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        DEEPSEEK_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        log(f"  LLM HTTP {exc.code}: {exc.read()[:300]!r}")
        return None
    except Exception as exc:  # noqa: BLE001
        log(f"  LLM 调用失败: {type(exc).__name__}: {exc}")
        return None
    try:
        content = body["choices"][0]["message"]["content"]
    except Exception:  # noqa: BLE001
        log(f"  LLM 返回异常: {str(body)[:300]}")
        return None
    content = re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.M).strip()
    try:
        return json.loads(content)
    except Exception:  # noqa: BLE001
        m = re.search(r"\{.*\}", content, flags=re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:  # noqa: BLE001
                pass
    log(f"  LLM 输出无法解析为 JSON: {content[:200]}")
    return None


def build_news_prompt(cands: list[dict], label: str, seen: set[str], avoid: set[str] | None = None) -> str:
    lines = []
    for i, c in enumerate(cands[:60], 1):
        when = c["published"].strftime("%m-%d %H:%M") if c.get("published") else "时间未知"
        lines.append(f"{i}. [{when}] {c['title']} | 来源: {c['source']} | {c['url']}")
    avoid_block = ""
    if avoid:
        avoid_block = "\n\n以下链接已用于另一个板块，绝对不能重复选用：\n" + "\n".join(sorted(avoid)[:30])
    return (
        f"下面是最近抓到的候选新闻，请从中选出 {PER_SECTION} 条「{label}」最值得中文读者知道的，"
        f"按重要性从高到低排序。\n\n"
        f"硬性要求：\n"
        f"- 只能从候选列表里选，url 必须原样复制，绝不编造或改写 URL。\n"
        f"- 同一件事只保留一条，优先一手来源和有实质进展的（模型/产品发布、重要开源更新、融资并购、安全与监管、关键研究结论）。\n"
        f"- 每条 summary 写 1-2 句中文，说清「发生了什么 + 为什么值得关注」，不要复述标题。\n"
        f"- 必须正好 {PER_SECTION} 条。\n"
        f"- 各条必须来自不同事件，不要同一家公司的同一件事占多条。\n"
        f"- 标注为「已推送过」的条目要跳过。\n\n"
        f"候选列表：\n" + "\n".join(lines) +
        f"\n\n已推送过（跳过）：{', '.join(sorted(seen)[:40]) or '无'}" + avoid_block +
        '\n\n按此 JSON 结构输出：{"items":[{"title":"中文标题（可保留英文专有名词）","url":"原样 URL","summary":"1-2 句中文","source":"来源名"}]}'
    )


def build_repo_prompt(cands: list[dict], label: str, seen: set[str], avoid: set[str] | None = None) -> str:
    lines = []
    for i, c in enumerate(cands[:60], 1):
        trend = f" | 日均涨星 ~{int(c.get('rising', 0))}" if c.get("rising") else ""
        lines.append(
            f"{i}. {c['name']} | ⭐{c['stars']} | 语言 {c['language'] or '?'} | 建库 {c['created']} | 最近提交 {c['pushed']}{trend} | {c['desc']} | {c['url']}"
        )
    avoid_block = ""
    if avoid:
        avoid_block = "\n\n以下仓库已用于另一个板块，绝对不能重复选用：\n" + "\n".join(sorted(avoid)[:30])
    scope = (
        "本板块专注「非 agent 方向」的 AI 项目：模型与权重、训练/推理基建、数据与评测、多模态与语音、RAG 与检索、向量库、部署与加速等。"
        if "agent" not in label.lower() and "Agent" not in label
        else "本板块专注 AI Agent / 智能体方向：agent 框架与运行时、MCP 与工具链、编码智能体、浏览器/电脑操作、多智能体协作、记忆与评测等。"
    )
    return (
        f"下面是 GitHub 候选仓库，请挑出 {PER_SECTION} 个「{label}」里最值得中文开发者关注的，按推荐度排序。\n"
        f"{scope}\n\n"
        f"硬性要求：\n"
        f"- 只能从候选里选，url 原样复制，不得编造。\n"
        f"- 优先「新出或正在快速上升」的项目：建库时间近、日均涨星快、最近有实质提交；"
        f"名单里日均涨星（rising）越高说明越热，请重点参考。\n"
        f"- 至多选 1 个星标超过 15 万的超头部项目，避免整份名单被老牌巨无霸占满。\n"
        f"- summary 写 1 句中文，说清「它是干什么的 + 为什么值得看」，允许有取舍判断，不要复述英文描述。\n"
        f"- 必须正好 {PER_SECTION} 个，且互不重复。\n\n"
        f"候选列表：\n" + "\n".join(lines) +
        f"\n\n已推送过（跳过）：{', '.join(sorted(seen)[:40]) or '无'}" + avoid_block +
        '\n\n按此 JSON 结构输出：{"items":[{"name":"owner/repo","url":"原样 URL","stars":1234,"summary":"1 句中文"}]}'
    )


def pad_news(selected: list[dict], pool: list[dict], seen: set[str]) -> list[dict]:
    out = list(selected)[:PER_SECTION]
    used = {i["url"] for i in out}
    for c in pool:
        if len(out) >= PER_SECTION:
            break
        if c["url"] in used or c["url"] in seen:
            continue
        used.add(c["url"])
        out.append({"title": c["title"], "url": c["url"], "summary": "", "source": c["source"]})
    return out


def pad_repos(selected: list[dict], pool: list[dict], seen: set[str]) -> list[dict]:
    out = list(selected)[:PER_SECTION]
    used = {i["url"] for i in out}
    for c in pool:
        if len(out) >= PER_SECTION:
            break
        if c["url"] in used or c["url"] in seen:
            continue
        used.add(c["url"])
        out.append({"name": c["name"], "url": c["url"], "stars": c["stars"], "summary": c["desc"]})
    return out


def sanitize_news(items: list[dict], pool: list[dict]) -> list[dict]:
    """只保留 url 确实来自候选池的条目（防模型编造）。"""
    by_url = {c["url"]: c for c in pool}
    out = []
    for it in items or []:
        url = (it.get("url") or "").strip()
        c = by_url.get(url)
        if not c:
            continue
        out.append({
            "title": (it.get("title") or c["title"]).strip(),
            "url": url,
            "summary": (it.get("summary") or "").strip(),
            "source": (it.get("source") or c["source"]).strip(),
        })
    return out


def sanitize_repos(items: list[dict], pool: list[dict]) -> list[dict]:
    by_url = {c["url"]: c for c in pool}
    by_name = {c["name"].lower(): c for c in pool}
    out = []
    for it in items or []:
        url = (it.get("url") or "").strip()
        c = by_url.get(url) or by_name.get((it.get("name") or "").strip().lower())
        if not c:
            continue
        out.append({
            "name": c["name"],
            "url": c["url"],
            "stars": c["stars"],
            "summary": (it.get("summary") or c["desc"]).strip(),
        })
    return out


# ---------------------------------------------------------------- 渲染


SECTIONS = [
    ("news_ai", "📰", "AI 热点"),
    ("repos_ai", "🔥", "GitHub 热门 AI 项目"),
    ("news_agent", "🤖", "Agent 热点"),
    ("repos_agent", "🛠", "GitHub 热门 Agent 项目"),
]


def render_html(date_label: str, data: dict) -> str:
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        "<style>",
        "body{margin:0;padding:24px 12px;background:#f5f6f8;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;color:#1a1d21;line-height:1.62}",
        ".wrap{max-width:680px;margin:0 auto;background:#fff;border-radius:14px;padding:28px 26px;box-shadow:0 1px 3px rgba(0,0,0,.07)}",
        "h1{font-size:20px;margin:0 0 4px}",
        ".sub{color:#6b7280;font-size:13px;margin-bottom:22px}",
        "h2{font-size:16px;margin:26px 0 10px;padding-bottom:6px;border-bottom:2px solid #eef0f3}",
        "ol{margin:0;padding-left:22px}",
        "li{margin:0 0 13px}",
        "a{color:#0b62d6;text-decoration:none}",
        ".meta{color:#6b7280;font-size:12.5px}",
        ".sum{color:#374151;font-size:13.5px}",
        ".foot{margin-top:26px;padding-top:14px;border-top:1px solid #eef0f3;color:#9ca3af;font-size:12px}",
        "</style></head><body><div class='wrap'>",
        f"<h1>🤖 AI / Agent 早报 · {html.escape(date_label)}</h1>",
        f"<div class='sub'>四板块各 {PER_SECTION} 条 · 由 GitHub Actions 自动生成并发送</div>",
    ]
    for key, emoji, title in SECTIONS:
        items = data.get(key) or []
        parts.append(f"<h2>{emoji} {title}（{len(items)}）</h2><ol>")
        for it in items:
            if key.startswith("news"):
                src = f" <span class='meta'>· {html.escape(it.get('source') or '')}</span>" if it.get("source") else ""
                sum_ = f"<div class='sum'>{html.escape(it['summary'])}</div>" if it.get("summary") else ""
                parts.append(
                    f"<li><a href='{html.escape(it['url'])}'>{html.escape(it['title'])}</a>{src}{sum_}</li>"
                )
            else:
                stars = f" ⭐{it.get('stars'):,}" if it.get("stars") else ""
                sum_ = f"<div class='sum'>{html.escape(it['summary'])}</div>" if it.get("summary") else ""
                parts.append(
                    f"<li><a href='{html.escape(it['url'])}'>{html.escape(it['name'])}</a>"
                    f"<span class='meta'>{stars}</span>{sum_}</li>"
                )
        parts.append("</ol>")
    parts.append("<div class='foot'>本邮件由 GitHub Actions 定时任务自动生成 · 数据来自公开 RSS / Hacker News / GitHub API</div>")
    parts.append("</div></body></html>")
    return "\n".join(parts)


def render_text(date_label: str, data: dict) -> str:
    lines = [f"🤖 AI / Agent 早报 · {date_label}", ""]
    for key, _emoji, title in SECTIONS:
        items = data.get(key) or []
        lines.append(f"== {title}（{len(items)}）==")
        for i, it in enumerate(items, 1):
            if key.startswith("news"):
                lines.append(f"{i}. {it['title']} — {it.get('source','')}")
                if it.get("summary"):
                    lines.append(f"   {it['summary']}")
                lines.append(f"   {it['url']}")
            else:
                lines.append(f"{i}. {it['name']} ⭐{it.get('stars',0):,}")
                if it.get("summary"):
                    lines.append(f"   {it['summary']}")
                lines.append(f"   {it['url']}")
        lines.append("")
    return "\n".join(lines)


def send_mail(subject: str, html_body: str, text_body: str) -> bool:
    user = os.environ.get("MAIL_USERNAME", "").strip()
    password = os.environ.get("MAIL_APP_PASSWORD", "").strip()
    if not (user and password):
        log("未配置 MAIL_USERNAME / MAIL_APP_PASSWORD，跳过发信")
        return False
    if not MAIL_TO:
        log("未配置 MAIL_TO（收件地址），跳过发信")
        return False
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com").strip()
    port = int(os.environ.get("SMTP_PORT", "465") or "465")
    sender = os.environ.get("MAIL_FROM", "").strip() or user
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"AI Agent 早报 <{sender}>"
    msg["To"] = MAIL_TO
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    ctx = ssl.create_default_context()
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=60) as s:
                s.login(user, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=60) as s:
                s.starttls(context=ctx)
                s.login(user, password)
                s.send_message(msg)
        log(f"邮件已发送（{host}:{port}）→ {MAIL_TO}")
        return True
    except Exception as exc:  # noqa: BLE001
        log(f"SMTP 发送失败: {type(exc).__name__}: {exc}")
        if host == "smtp.gmail.com" and port == 465:
            log("改用 587 STARTTLS 重试...")
            try:
                with smtplib.SMTP(host, 587, timeout=60) as s:
                    s.starttls(context=ctx)
                    s.login(user, password)
                    s.send_message(msg)
                log(f"邮件已发送（{host}:587）→ {MAIL_TO}")
                return True
            except Exception as exc2:  # noqa: BLE001
                log(f"587 也失败: {type(exc2).__name__}: {exc2}")
        return False


# ---------------------------------------------------------------- 主流程


def main() -> int:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    date_label = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d")
    raw_state = read_state_raw()
    seen = load_seen()
    log(f"已推链接记忆: {len(seen)} 条")

    log("采集订阅源...")
    news_all = dedupe_news(collect_feed_news() + collect_hn_news())
    log(f"去重后新闻候选: {len(news_all)} 条")
    news_ai_pool = filter_news(news_all, "ai")
    news_agent_pool = filter_news(news_all, "agent")
    log(f"AI 候选 {len(news_ai_pool)} / Agent 候选 {len(news_agent_pool)}")

    log("采集 GitHub 项目...")
    repos_all = collect_repos("ai") + collect_repos("agent")
    merged = list({r["name"]: r for r in repos_all}.values())
    repos_ai_pool = sorted([r for r in merged if not r["agentish"]], key=lambda r: r["rising"], reverse=True)
    repos_agent_pool = sorted([r for r in merged if r["agentish"]], key=lambda r: r["rising"], reverse=True)
    log(f"AI 仓库 {len(repos_ai_pool)} / Agent 仓库 {len(repos_agent_pool)}（按日均涨星排序）")

    data: dict[str, list[dict]] = {}
    plan = [
        ("news_ai", "AI 热点新闻", news_ai_pool, build_news_prompt, sanitize_news, pad_news),
        ("news_agent", "AI Agent 热点新闻", news_agent_pool, build_news_prompt, sanitize_news, pad_news),
        ("repos_ai", "AI 开源项目", repos_ai_pool, build_repo_prompt, sanitize_repos, pad_repos),
        ("repos_agent", "Agent 开源项目", repos_agent_pool, build_repo_prompt, sanitize_repos, pad_repos),
    ]
    taken: set[str] = set()  # 跨板块去重：已选进任一板块的链接不再出现在其他板块
    for key, label, pool, prompt_fn, sanitize_fn, pad_fn in plan:
        pool = [c for c in pool if c["url"] not in taken and c["url"] not in seen]
        selected: list[dict] = []
        if api_key and pool:
            log(f"LLM 选编 [{key}] （候选 {len(pool)}，已排除 {len(taken)} 条跨板块重复）...")
            out = llm_json(api_key, SYSTEM_PROMPTS[key], prompt_fn(pool, label, seen, taken))
            if out:
                selected = sanitize_fn(out.get("items"), pool)
                log(f"  LLM 返回并校验通过 {len(selected)} 条")
        chosen = pad_fn(selected, pool, seen)
        log(f"  [{key}] 最终 {len(chosen)} 条")
        data[key] = chosen
        taken |= {c["url"] for c in chosen}

    html_body = render_html(date_label, data)
    text_body = render_text(date_label, data)

    if DRY_RUN:
        (ROOT / "preview.html").write_text(html_body, encoding="utf-8")
        (ROOT / "preview.txt").write_text(text_body, encoding="utf-8")
        log("DRY_RUN：已写出 preview.html / preview.txt")
    else:
        counts = " / ".join(f"{t}{len(data[k])}" for k, _e, t in SECTIONS)
        send_mail(f"AI / Agent 早报 · {date_label}（{counts}）", html_body, text_body)

    save_seen([it["url"] for k, _e, _t in SECTIONS for it in data.get(k, [])], raw_state)
    missing = [k for k, _e, _t in SECTIONS if len(data.get(k, [])) < PER_SECTION]
    if missing:
        log(f"⚠ 以下板块不足 {PER_SECTION} 条: {missing}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
