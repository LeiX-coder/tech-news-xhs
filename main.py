#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
海外科技新闻监控 + 小红书文案生成
数据源：
1. SEC 8-K / 6-K 申报文件
2. TechCrunch 科技新闻（仅保留监控公司相关）
3. 公司官网新闻发布（Press Release）

【本版核心改动】
所有送进 DeepSeek 的材料都必须是「抓回来的正文」，不再只传标题+链接。
模型没有浏览器，传什么它才知道什么；传标题它就只能编正文。
同时强制提取真实发布日期，抓不到日期就不许模型写日期。
"""

import os
import re
import json
import time
import calendar
import hashlib
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from openai import OpenAI
import feedparser

# ==================== 配置区域 ====================

# SEC 强制要求：必须是真实的「名字 邮箱」（只用于 sec.gov）
SEC_IDENTITY = "xiaolei xiaolei12555@126.com"

# 访问公司官网 / 新闻站用普通浏览器 UA，不要带 SEC 身份
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

# 公司基本名称（用于显示）
COMPANIES = {
    "INTC": "英特尔 Intel",
    "NOK": "诺基亚 Nokia",
    "AAOI": "Applied Optoelectronics",
    "LITE": "Lumentum Holdings",
    "MU": "美光 Micron",
    "AAPL": "苹果 Apple",
    "MSFT": "微软 Microsoft",
    "NVDA": "英伟达 Nvidia",
    "GOOGL": "谷歌 Alphabet",
    "AMZN": "亚马逊 Amazon",
    "META": "Meta",
    "AVGO": "博通 Broadcom",
    "TSM": "台积电 TSMC",
    "AMD": "超微半导体 AMD",
    "QCOM": "高通 Qualcomm",
    "TXN": "德州仪器 TI",
    "AMAT": "应用材料 Applied Materials",
    "LRCX": "泛林半导体 Lam Research",
    "KLAC": "科磊 KLA",
    "ADI": "亚德诺 Analog Devices",
    "NXPI": "恩智浦 NXP",
    "MRVL": "美满 Marvell",
    "ASML": "阿斯麦 ASML",
    "SMCI": "超微电脑 Super Micro",
    "WDC": "西部数据 Western Digital",
    "SNPS": "新思科技 Synopsys",
    "CDNS": "楷登电子 Cadence",
    "CSCO": "思科 Cisco",
    "PANW": "Palo Alto Networks",
    "CRWD": "CrowdStrike",
    "ZS": "Zscaler",
    "FTNT": "Fortinet",
    "PLTR": "Palantir",
    "ANET": "Arista Networks",
    "STX": "希捷 Seagate",
}

# 公司官网新闻页 URL
COMPANY_NEWS_URLS = {
    "INTC": "https://www.intel.com/content/www/us/en/newsroom/news.html",
    "NOK": "https://www.nokia.com/about-us/news/releases/",
    "AAOI": "https://investors.ao-inc.com/news-releases",
    "LITE": "https://investor.lumentum.com/news-releases",
    "MU": "https://investors.micron.com/news-releases",
    "AAPL": "https://www.apple.com/newsroom/",
    "MSFT": "https://news.microsoft.com/",
    "NVDA": "https://nvidianews.nvidia.com/",
    "GOOGL": "https://blog.google/",
    "AMZN": "https://www.aboutamazon.com/news",
    "META": "https://about.fb.com/news/",
    "AVGO": "https://investors.broadcom.com/news-releases",
    "TSM": "https://pr.tsmc.com/english/news",
    "AMD": "https://ir.amd.com/news-releases",
    "QCOM": "https://www.qualcomm.com/news/releases",
    "TXN": "https://investor.ti.com/news-releases",
    "AMAT": "https://investors.appliedmaterials.com/news-releases",
    "LRCX": "https://investor.lamresearch.com/news-releases",
    "KLAC": "https://ir.kla.com/news-releases",
    "ADI": "https://investors.analog.com/news-releases",
    "NXPI": "https://investors.nxp.com/news-releases",
    "MRVL": "https://investors.marvell.com/news-releases",
    "ASML": "https://www.asml.com/en/news/press-releases",
    "SMCI": "https://www.supermicro.com/en/about/newsroom",
    "WDC": "https://investor.wdc.com/news-releases",
    "SNPS": "https://www.synopsys.com/company/news.html",
    "CDNS": "https://www.cadence.com/en_US/home/company/news-events.html",
    "CSCO": "https://newsroom.cisco.com/",
    "PANW": "https://www.paloaltonetworks.com/company/newsroom",
    "CRWD": "https://www.crowdstrike.com/news/",
    "ZS": "https://www.zscaler.com/company/newsroom",
    "FTNT": "https://www.fortinet.com/corporate/about-us/newsroom",
    "PLTR": "https://www.palantir.com/news/",
    "ANET": "https://www.arista.com/en/news",
    "STX": "https://investors.seagate.com/news-releases",
}

# 官网 RSS 覆盖表。留空则自动从列表页的 <link rel="alternate"> 发现。
# 自动发现失败、或想强制指定时，在这里写死，例如：
#   "NVDA": "https://nvidianews.nvidia.com/releases.xml",
COMPANY_FEEDS = {}

# 官网解析选择器（可针对不同公司定制）
NEWS_SELECTORS = {
    "default": {
        "link_selector": "a[href*='news'], a[href*='release'], a[href*='press']",
        "title_selector": None,
        "exclude_patterns": [r'\.(jpg|png|gif|pdf|xml|json)$', r'\bnewsletter\b', r'\brss\b']
    },
    "INTC": {
        "link_selector": "a[href*='/news/'], a[href*='/press/']",
        "title_selector": "h3, h2, .title",
        "exclude_patterns": [r'\.(jpg|png|gif|pdf|xml|json)$', r'\bnewsletter\b']
    },
    "AMD": {
        "link_selector": "a[href*='news-releases']",
        "title_selector": ".title, h3",
        "exclude_patterns": [r'\.(jpg|png|gif|pdf|xml|json)$']
    },
    "NVDA": {
        "link_selector": "a[href*='/news/']",
        "title_selector": ".headline, h2",
        "exclude_patterns": [r'\.(jpg|png|gif|pdf|xml|json)$']
    },
    "AAPL": {
        "link_selector": "a[href*='/newsroom/']",
        "title_selector": "h2, h3",
        "exclude_patterns": [r'\.(jpg|png|gif|pdf|xml|json)$']
    },
    "MSFT": {
        "link_selector": "a[href*='/news/']",
        "title_selector": "h2, h3",
        "exclude_patterns": [r'\.(jpg|png|gif|pdf|xml|json)$']
    },
    "GOOGL": {
        "link_selector": "a[href*='/blog/']",
        "title_selector": "h3, h2",
        "exclude_patterns": [r'\.(jpg|png|gif|pdf|xml|json)$']
    },
    "AMZN": {
        "link_selector": "a[href*='/news/']",
        "title_selector": "h2, h3",
        "exclude_patterns": [r'\.(jpg|png|gif|pdf|xml|json)$']
    },
    "META": {
        "link_selector": "a[href*='/news/']",
        "title_selector": "h2, h3",
        "exclude_patterns": [r'\.(jpg|png|gif|pdf|xml|json)$']
    },
    "TSM": {
        "link_selector": "a[href*='/english/news']",
        "title_selector": ".title, h3",
        "exclude_patterns": [r'\.(jpg|png|gif|pdf|xml|json)$']
    },
    "ASML": {
        "link_selector": "a[href*='/news/press-releases']",
        "title_selector": "h2, h3",
        "exclude_patterns": [r'\.(jpg|png|gif|pdf|xml|json)$']
    },
    "SMCI": {
        "link_selector": "a[href*='/newsroom/']",
        "title_selector": "h2, h3",
        "exclude_patterns": [r'\.(jpg|png|gif|pdf|xml|json)$']
    },
}

DAYS_LOOKBACK = 3

# 正文长度门槛：低于这个字符数一律不生成文案，只登记线索
MIN_ARTICLE_CHARS = 400
# 送进模型的正文上限，防止 token 爆炸
MAX_ARTICLE_CHARS = 12000
# 每家官网最多检查几条候选链接
MAX_WEB_CANDIDATES = 8
# 官网阶段总时间预算（秒）。超了就停，避免 Actions 整个 job 被 timeout 杀掉、
# state 来不及提交。SEC 阶段不设限，那条线才是主力信源。
WEB_PHASE_BUDGET_SEC = 1200

# ---- 保留策略 ----
# output/ 只留 summary_*.md，且只留最近 N 天（按文件名里的日期，不按 mtime）
OUTPUT_RETENTION_DAYS = 3
# 是否为每条内容单独写一个 .md。关掉后所有文案都只存在于 summary 里。
KEEP_INDIVIDUAL_FILES = False
# state 的保留期必须 > DAYS_LOOKBACK，否则一条申报还在回溯窗口里、
# 去重记录却已经被裁掉，会重复处理、重复调 API。
STATE_RETENTION_DAYS = DAYS_LOOKBACK + 4

# 白名单 Item
WANTED_ITEMS = {
    "1.01": "签署重大协议",
    "1.02": "终止重大协议",
    "1.03": "破产或接管",
    "2.01": "完成收购或处置资产",
    "2.02": "经营业绩与财务状况（财报）",
    "2.03": "产生直接财务义务",
    "2.04": "触发条款导致债务加速到期",
    "2.05": "退出或处置业务的成本",
    "2.06": "重大资产减值",
    "3.01": "退市通知或上市规则不合规",
    "4.01": "更换会计师事务所",
    "4.02": "此前财报不可依赖（财务重述）",
    "5.02": "董事或高管任免",
}

FILLER_ITEMS = {"7.01", "8.01", "9.01", "5.03", "5.07", "5.08"}
ALERT_ITEMS = {"4.02", "1.03", "3.01"}

EXEC_CHANGE_KEYWORDS = [
    "resign", "resignation", "step down", "stepping down", "depart",
    "terminate", "termination", "effective immediately", "transition",
    "chief executive", "chief financial", "chief operating",
    "interim", "successor", "retire", "retirement",
]

TECHCRUNCH_RSS_URL = "https://techcrunch.com/feed/"

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-v4-flash"

ET = ZoneInfo("America/New_York")
SEC = "https://www.sec.gov"

STATE_DIR = Path("state")
OUTPUT_DIR = Path("output")
SEEN_FILE = STATE_DIR / "seen_accessions.json"

# ==================== HTTP 层 ====================

_lock = threading.Lock()
_last_request = [0.0]
MIN_INTERVAL = 0.12

HEADERS = {
    "User-Agent": SEC_IDENTITY,
    "Accept-Encoding": "gzip, deflate",
}

WEB_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}


def sec_get(url: str, max_retries: int = 3, timeout: int = 45):
    """只用于 sec.gov：带 SEC 身份 UA，全局限速 ~8 req/s。"""
    for attempt in range(max_retries):
        with _lock:
            gap = time.monotonic() - _last_request[0]
            if gap < MIN_INTERVAL:
                time.sleep(MIN_INTERVAL - gap)
            _last_request[0] = time.monotonic()

        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
        except requests.RequestException:
            if attempt == max_retries - 1:
                raise
            time.sleep(2 ** attempt)
            continue

        if r.status_code in (429, 403, 503):
            wait = 5 * (attempt + 1)
            print(f"    ⏳ {r.status_code}，{wait}s 后重试: {url}")
            time.sleep(wait)
            continue

        r.raise_for_status()
        return r

    raise RuntimeError(f"请求失败（已重试 {max_retries} 次）: {url}")


def web_get(url: str, max_retries: int = 2, timeout: int = 15):
    """用于公司官网、新闻站：普通浏览器 UA。"""
    for attempt in range(max_retries):
        try:
            r = requests.get(url, headers=WEB_HEADERS, timeout=timeout,
                             allow_redirects=True)
        except requests.RequestException:
            if attempt == max_retries - 1:
                raise
            time.sleep(2 ** attempt)
            continue

        if r.status_code in (429, 503):
            time.sleep(3 * (attempt + 1))
            continue

        r.raise_for_status()
        # 避免把 PDF / 图片当 HTML 解析
        ctype = r.headers.get("Content-Type", "")
        if "html" not in ctype and "xml" not in ctype and ctype:
            raise RuntimeError(f"非 HTML 内容（{ctype}）")
        return r

    raise RuntimeError(f"请求失败（已重试 {max_retries} 次）: {url}")

# ==================== 基础工具 ====================

_ticker_cache = {}


def load_ticker_map() -> dict:
    if _ticker_cache:
        return _ticker_cache
    data = sec_get(f"{SEC}/files/company_tickers.json").json()
    for row in data.values():
        _ticker_cache[row["ticker"].upper()] = int(row["cik_str"])
    return _ticker_cache


def clean_html(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all(["ix:header", "ix:hidden", "script", "style"]):
        tag.decompose()
    for tag in soup.find_all(style=re.compile(r"display\s*:\s*none", re.I)):
        tag.decompose()
    text = soup.get_text("\n").replace("\xa0", " ")
    lines = [ln.strip() for ln in text.splitlines()]
    text = "\n".join(ln for ln in lines if ln)
    return re.sub(r"\n{3,}", "\n\n", text)


def parse_accepted_et(raw: str, fallback_date: str) -> datetime:
    if raw:
        try:
            naive = datetime.fromisoformat(raw.replace("Z", "").split(".")[0])
            return naive.replace(tzinfo=ET)
        except ValueError:
            pass
    return datetime.fromisoformat(fallback_date).replace(tzinfo=ET)


class SeenStore:
    """
    去重记录。存成 {id: "YYYY-MM-DD"}，带上日期才能按时间裁剪。
    对外仍然是 `x in seen` / `seen.add(x)`，调用处不用改。
    兼容旧的扁平数组格式（首次读到会自动迁移，日期记为今天）。
    """

    def __init__(self, data=None):
        self._d = dict(data or {})

    def __contains__(self, key):
        return key in self._d

    def __len__(self):
        return len(self._d)

    def add(self, key):
        # setdefault：已存在就保留原始日期，不要每次运行都刷新成今天，
        # 否则永远裁不掉。
        self._d.setdefault(key, datetime.now(ET).strftime("%Y-%m-%d"))

    def prune(self, days: int) -> int:
        cutoff = (datetime.now(ET) - timedelta(days=days)).strftime("%Y-%m-%d")
        before = len(self._d)
        self._d = {k: v for k, v in self._d.items() if v >= cutoff}
        return before - len(self._d)

    def to_dict(self) -> dict:
        return dict(sorted(self._d.items()))


def load_seen() -> SeenStore:
    if not SEEN_FILE.exists():
        return SeenStore()
    try:
        raw = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    except Exception:
        return SeenStore()

    if isinstance(raw, dict):
        return SeenStore(raw)

    # 旧格式：扁平数组，没有日期。迁移时统一记为今天，
    # 意味着迁移后第一次裁剪要等 STATE_RETENTION_DAYS 天，属预期行为。
    if isinstance(raw, list):
        today = datetime.now(ET).strftime("%Y-%m-%d")
        print(f"  🔄 seen 迁移：{len(raw)} 条旧格式记录 → 带日期格式")
        return SeenStore({k: today for k in raw})

    return SeenStore()


def save_seen(seen: SeenStore):
    STATE_DIR.mkdir(exist_ok=True)
    SEEN_FILE.write_text(
        json.dumps(seen.to_dict(), ensure_ascii=False, indent=0, sort_keys=True),
        encoding="utf-8",
    )

# ==================== 通用正文 / 日期提取 ====================

_JUNK_TAGS = ["script", "style", "noscript", "nav", "header", "footer",
              "aside", "form", "iframe", "svg", "button", "figure", "video"]

_JUNK_CLASS = re.compile(
    r"(cookie|consent|newsletter|subscribe|share|social|related|recommend|"
    r"breadcrumb|menu|sidebar|nav-|footer|header|banner|promo|popup|modal)", re.I)

ARTICLE_SELECTORS = [
    "article",
    '[itemprop="articleBody"]',
    ".article-body", ".article__body", ".articleBody",
    ".press-release", ".press-release-body", ".news-release",
    ".entry-content", ".post-content", ".post-body",
    ".rich-text", ".body-copy", ".content-body",
    "main",
]

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_TEXT_DATE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+"
    r"(\d{1,2})\s*,?\s+(\d{4})\b", re.I)

_TEXT_DATE_EU = re.compile(
    r"\b(\d{1,2})\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+"
    r"(\d{4})\b", re.I)


def parse_iso_dt(raw: str):
    """把各种 ISO-ish 时间串转成美东时区 datetime；失败返回 None。"""
    if not raw:
        return None
    s = str(raw).strip()
    s = s.replace("Z", "+00:00")
    s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
        if not m:
            return None
        dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ET)
    return dt.astimezone(ET)


def parse_text_date(text: str):
    """从正文开头找 'July 23, 2026' / '23 July 2026' 这类日期。"""
    if not text:
        return None
    head = text[:1200]
    m = _TEXT_DATE.search(head)
    if m:
        mon = _MONTHS.get(m.group(1)[:3].lower())
        if mon:
            try:
                return datetime(int(m.group(3)), mon, int(m.group(2)), tzinfo=ET)
            except ValueError:
                pass
    m = _TEXT_DATE_EU.search(head)
    if m:
        mon = _MONTHS.get(m.group(2)[:3].lower())
        if mon:
            try:
                return datetime(int(m.group(3)), mon, int(m.group(1)), tzinfo=ET)
            except ValueError:
                pass
    return None


def extract_published(soup: BeautifulSoup):
    """从 meta / time / JSON-LD 里找发布时间。必须在 extract_article_text 之前调用。"""
    meta_keys = [
        ("property", "article:published_time"),
        ("property", "og:article:published_time"),
        ("property", "article:modified_time"),
        ("name", "publishdate"),
        ("name", "pubdate"),
        ("name", "date"),
        ("name", "dc.date"),
        ("name", "dc.date.issued"),
        ("name", "parsely-pub-date"),
        ("itemprop", "datePublished"),
    ]
    for attr, val in meta_keys:
        tag = soup.find("meta", attrs={attr: re.compile(f"^{re.escape(val)}$", re.I)})
        if tag and tag.get("content"):
            dt = parse_iso_dt(tag["content"])
            if dt:
                return dt

    # JSON-LD
    for script in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        raw = script.string or script.get_text() or ""
        m = re.search(r'"datePublished"\s*:\s*"([^"]+)"', raw)
        if m:
            dt = parse_iso_dt(m.group(1))
            if dt:
                return dt

    # <time datetime="...">
    for t in soup.find_all("time"):
        if t.get("datetime"):
            dt = parse_iso_dt(t["datetime"])
            if dt:
                return dt

    return None


def _paragraph_text(node) -> str:
    blocks = node.find_all(["p", "li", "h1", "h2", "h3", "h4", "td", "blockquote"])
    if blocks:
        lines = [b.get_text(" ", strip=True) for b in blocks]
    else:
        lines = [node.get_text("\n", strip=True)]
    return "\n".join(ln for ln in lines if len(ln) > 1)


def _tidy(text: str) -> str:
    text = text.replace("\xa0", " ")
    seen_lines = set()
    out = []
    for ln in text.splitlines():
        ln = re.sub(r"[ \t]+", " ", ln).strip()
        if not ln:
            continue
        if ln in seen_lines and len(ln) < 120:
            continue
        seen_lines.add(ln)
        out.append(ln)
    return "\n".join(out)


def extract_article_text(soup: BeautifulSoup) -> str:
    """会破坏性修改 soup，调用前先取日期和标题。"""
    for t in soup.find_all(_JUNK_TAGS):
        t.decompose()
    for t in soup.find_all(attrs={"class": _JUNK_CLASS}):
        t.decompose()
    for t in soup.find_all(attrs={"id": _JUNK_CLASS}):
        t.decompose()

    best, best_len = "", 0
    for sel in ARTICLE_SELECTORS:
        try:
            nodes = soup.select(sel)
        except Exception:
            continue
        for node in nodes:
            txt = _paragraph_text(node)
            if len(txt) > best_len:
                best, best_len = txt, len(txt)

    if best_len < 300:
        body = soup.body or soup
        fallback = _paragraph_text(body)
        if len(fallback) > best_len:
            best = fallback

    return _tidy(best)


def extract_title(soup: BeautifulSoup) -> str:
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        return og["content"].strip()
    h1 = soup.find("h1")
    if h1:
        t = h1.get_text(" ", strip=True)
        if t:
            return t
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    return ""


def fetch_article(url: str) -> dict:
    """抓一篇文章的正文 + 发布时间 + 标题。这是本次修改的核心函数。"""
    r = web_get(url)
    soup = BeautifulSoup(r.text, "lxml")

    published = extract_published(soup)      # 必须先取
    title = extract_title(soup)
    text = extract_article_text(soup)        # 之后 soup 已被破坏

    if published is None:
        published = parse_text_date(text)

    return {
        "url": r.url,
        "title": title,
        "text": text[:MAX_ARTICLE_CHARS],
        "published": published,
        "truncated": len(text) > MAX_ARTICLE_CHARS,
    }

# ==================== SEC 抓取 ====================


def list_filings(cik: int, forms=("8-K", "6-K"), lookback_days=3):
    url = f"https://data.sec.gov/submissions/CIK{cik:010d}.json"
    data = sec_get(url).json()
    rec = data["filings"]["recent"]

    cutoff = datetime.now(ET) - timedelta(days=lookback_days)
    n = len(rec["form"])
    items_col = rec.get("items", [""] * n)

    out = []
    for i in range(n):
        if rec["form"][i] not in forms:
            continue

        accepted = parse_accepted_et(
            rec["acceptanceDateTime"][i] if i < len(rec.get("acceptanceDateTime", [])) else "",
            rec["filingDate"][i],
        )
        if accepted < cutoff:
            continue

        acc_dash = rec["accessionNumber"][i]
        acc = acc_dash.replace("-", "")
        base = f"{SEC}/Archives/edgar/data/{cik}/{acc}"
        raw_items = items_col[i] if i < len(items_col) else ""
        items = [x.strip() for x in raw_items.split(",") if x.strip()]

        out.append({
            "cik": cik,
            "company_name": data["name"],
            "form": rec["form"][i],
            "accession": acc_dash,
            "filed": rec["filingDate"][i],
            "accepted_et": accepted,
            "items": items,
            "base": base,
            "full_txt_url": f"{base}/{acc_dash}.txt",
            "primary_url": f"{base}/{rec['primaryDocument'][i]}",
            "doc_desc": (rec.get("primaryDocDescription") or [""] * n)[i] or "",
        })

    return out


def list_documents(base: str, accession: str):
    html = sec_get(f"{base}/{accession}-index.htm").text
    soup = BeautifulSoup(html, "lxml")

    docs = []
    for row in soup.select("table.tableFile tr"):
        cells = [c.get_text(" ", strip=True) for c in row.find_all("td")]
        link = row.find("a")
        if len(cells) < 4 or not link:
            continue
        href = link.get("href", "").split("#")[0]
        if href.startswith("/ix?doc="):
            href = href[len("/ix?doc="):]
        docs.append({
            "desc": cells[1],
            "doc": link.get_text(strip=True),
            "type": cells[3],
            "url": SEC + href if href.startswith("/") else href,
        })
    return docs


_MAJOR_CANDIDATES = ["99", "32", "31", "24", "23", "21", "10",
                     "1", "2", "3", "4", "5", "7", "8"]


def _exhibit_number(s: str):
    s = s.lower()

    if re.search(r"ex[\-_]?(\d{3})\.(ins|sch|cal|def|lab|pre)", s):
        return (int(re.search(r"ex[\-_]?(\d{3})", s).group(1)), 0)

    s = re.sub(r"\.(htm|html|txt|xml|xsd|pdf|jpe?g|png|gif)$", "", s)

    m = re.search(r"ex[\-_]?(\d+)(?:[\.\-_](\d+))?", s)
    if not m:
        return None
    digits, minor = m.group(1), m.group(2)

    if minor is not None:
        return (int(digits), int(minor))

    if len(digits) >= 3:
        for major in _MAJOR_CANDIDATES:
            if digits.startswith(major) and len(digits) > len(major):
                return (int(major), int(digits[len(major):]))
    return (int(digits), 0)


def find_exhibit(docs, major: int, minor: int):
    for d in docs:
        for field in (d["type"], d["doc"]):
            num = _exhibit_number(field)
            if num and num[0] == major and (num[1] == minor or minor == 0):
                return d
    return None


def extract_press_release(text: str, head_chars=4500, guidance_window=2500) -> str:
    if len(text) <= head_chars + guidance_window:
        return text

    head = text[:head_chars]
    rest = text[head_chars:]
    m = re.search(
        r"(?i)\b(outlook|guidance|forecast|for the (first|second|third|fourth) quarter"
        r"|expects? (revenue|to report))",
        rest,
    )
    if m:
        seg = rest[m.start(): m.start() + guidance_window]
        return f"{head}\n\n...（中间部分省略）...\n\n【指引 / Outlook 段落】\n{seg}"
    return head


def build_content(filing: dict) -> dict:
    """
    改动：不再只在 2.02 / 6-K 时才找 EX-99.x。
    8-K 正文常常只是一页封面（"详见附件 99.1"），真正的数字全在新闻稿附件里。
    """
    items = filing["items"]
    parts = []

    body = clean_html(sec_get(filing["primary_url"]).text)
    if len(body) < 100:
        raise RuntimeError(f"正文提取过短（{len(body)} 字符），疑似解析失败")
    parts.append(f"【{filing['form']} 正文】\n{body[:6000]}")

    exhibits_used = []
    docs = None

    try:
        docs = list_documents(filing["base"], filing["accession"])
    except Exception as e:
        docs = None
        print(f"    ⚠️ 附件清单读取失败: {e}")

    ex = None
    if docs:
        ex = find_exhibit(docs, 99, 1) or find_exhibit(docs, 99, 0)

    if ex:
        pr = clean_html(sec_get(ex["url"]).text)
        if len(pr) >= 200:
            parts.append(f"\n【新闻稿原文 {ex['type']}】\n{extract_press_release(pr)}")
            exhibits_used.append(ex["url"])
        elif "2.02" in items:
            raise RuntimeError("Item 2.02 的 EX-99.x 正文过短，疑似解析失败")
    elif "2.02" in items:
        raise RuntimeError("Item 2.02 但未找到 EX-99.x 新闻稿")

    return {
        "text": "\n\n".join(parts),
        "exhibits": exhibits_used,
        "documents": docs,
    }


def should_process(filing: dict) -> tuple:
    items = filing["items"]

    if filing["form"] == "6-K":
        haystack = f"{filing.get('doc_desc', '')} {filing.get('primary_url', '')}".lower()

        ROUTINE_KEYWORDS = [
            "month end", "monthend", "monthly",
            "insider", "shareholding", "share holding",
            "beneficial", "exempt",
            "total voting rights", "share capital",
            "transaction in own shares", "buy-back", "buyback",
            "director", "pdmr",
        ]

        for kw in ROUTINE_KEYWORDS:
            if kw in haystack:
                return "6-K_ROUTINE", f"6-K 常规披露（{kw}）"

        return True, "6-K"

    if not items:
        return True, "⚠️ items 为空（数据异常，保留复核）"

    hit = set(items) & set(WANTED_ITEMS)
    if not hit:
        return False, f"仅含附属条目: {','.join(items)}"

    if hit == {"5.02"}:
        return "CHECK_5_02", "5.02 待关键词确认"

    return True, ",".join(sorted(hit))

# ==================== TechCrunch RSS ====================

ALIASES = {
    "INTC":  ["intel"],
    "NOK":   ["nokia"],
    "AAOI":  ["applied optoelectronics"],
    "LITE":  ["lumentum"],
    "MU":    ["micron"],
    "AAPL":  ["apple"],
    "MSFT":  ["microsoft"],
    "NVDA":  ["nvidia"],
    "GOOGL": ["google", "alphabet", "deepmind"],
    "AMZN":  ["amazon", "aws"],
    "META":  ["meta", "facebook", "instagram", "whatsapp"],
    "AVGO":  ["broadcom", "vmware"],
    "TSM":   ["tsmc", "taiwan semiconductor"],
    "AMD":   ["amd", "advanced micro devices"],
    "QCOM":  ["qualcomm", "snapdragon"],
    "TXN":   ["texas instruments"],
    "AMAT":  ["applied materials"],
    "LRCX":  ["lam research"],
    "KLAC":  ["kla corporation", "kla-tencor"],
    "ADI":   ["analog devices"],
    "NXPI":  ["nxp semiconductors", "nxp"],
    "MRVL":  ["marvell"],
    "ASML":  ["asml"],
    "SMCI":  ["supermicro", "super micro"],
    "WDC":   ["western digital", "sandisk"],
    "SNPS":  ["synopsys"],
    "CDNS":  ["cadence design", "cadence"],
    "CSCO":  ["cisco"],
    "PANW":  ["palo alto networks"],
    "CRWD":  ["crowdstrike"],
    "ZS":    ["zscaler"],
    "FTNT":  ["fortinet"],
    "PLTR":  ["palantir"],
    "ANET":  ["arista networks", "arista"],
    "STX":   ["seagate"],
}

_ALIAS_PATTERNS = {
    ticker: [re.compile(r"(?<![a-z0-9])" + re.escape(a) + r"(?![a-z0-9])", re.I)
             for a in names]
    for ticker, names in ALIASES.items()
}


def extract_matched_companies(text: str) -> list:
    matched = []
    for ticker, pats in _ALIAS_PATTERNS.items():
        if any(p.search(text) for p in pats):
            matched.append(ticker)
    return matched


def _stable_id(prefix: str, key: str) -> str:
    return f"{prefix}_{hashlib.sha1(key.encode('utf-8')).hexdigest()[:16]}"


def _rss_published_et(entry):
    st = entry.get("published_parsed") or entry.get("updated_parsed")
    if not st:
        return None
    try:
        return datetime.fromtimestamp(calendar.timegm(st), tz=timezone.utc).astimezone(ET)
    except Exception:
        return None


def _entry_body(entry) -> str:
    """优先用 content:encoded（常常是全文），退回 summary。"""
    for c in entry.get("content", []) or []:
        val = c.get("value", "")
        if val:
            txt = clean_html(val)
            if txt:
                return txt
    return clean_html(entry.get("summary", ""))


def fetch_techcrunch_news(limit: int = 30):
    """
    改动：
    1. 先取 content:encoded 全文，不够长再回源抓文章正文；
    2. 用 RSS 的真实发布时间，不再用 now()；
    3. 超出回溯窗口的旧文直接丢掉。
    """
    try:
        feed = feedparser.parse(TECHCRUNCH_RSS_URL)
    except Exception as e:
        print(f"  ⚠️ TechCrunch RSS 抓取失败: {e}")
        return []

    if getattr(feed, "bozo", 0) and not feed.entries:
        print(f"  ⚠️ RSS 解析异常: {getattr(feed, 'bozo_exception', '未知')}")
        return []

    cutoff = datetime.now(ET) - timedelta(days=DAYS_LOOKBACK)
    out = []

    for entry in feed.entries[:limit]:
        title = entry.get("title", "")
        link = entry.get("link", "")
        body = _entry_body(entry)
        published = _rss_published_et(entry)

        if published and published < cutoff:
            continue

        matched = extract_matched_companies(f"{title} {body}")
        if not matched:
            continue

        # RSS 摘要太短就回源抓正文
        if len(body) < MIN_ARTICLE_CHARS and link:
            try:
                art = fetch_article(link)
                if len(art["text"]) > len(body):
                    body = art["text"]
                if published is None:
                    published = art["published"]
                print(f"    🔗 回源抓取正文: {title[:45]} → {len(body)} 字符")
            except Exception as e:
                print(f"    ⚠️ 回源失败（{title[:35]}）: {e}")

        out.append({
            "id": _stable_id("TC", link or title),
            "title": title,
            "link": link,
            "body": body[:MAX_ARTICLE_CHARS],
            "published": published,
            "source": "TechCrunch",
            "matched_companies": matched,
            "thin": len(body) < MIN_ARTICLE_CHARS,
        })
    return out

# ==================== 官网新闻抓取 ====================

_NAV_TITLES = re.compile(
    r"^(all\s+news|news|newsroom|press|press\s+releases?|media|media\s+contacts?|"
    r"investor\s+relations|subscribe|contact\s+us|more|read\s+more|learn\s+more|"
    r"view\s+all|archive|rss|home)$", re.I)


_FEED_TYPES = re.compile(r"application/(rss|atom)\+xml", re.I)


def discover_feed_url(html: str, base_url: str):
    """
    从列表页的 <link rel="alternate" type="application/rss+xml"> 里找 RSS。
    走 RSS 比爬 HTML 稳得多：自带发布时间、常常自带全文，
    而且不用为每家公司猜 DOM 结构。
    """
    soup = BeautifulSoup(html, "lxml")
    for link in soup.find_all("link"):
        rel = " ".join(link.get("rel") or []).lower()
        typ = link.get("type") or ""
        href = link.get("href")
        if not href or not _FEED_TYPES.search(typ):
            continue
        if rel and "alternate" not in rel:
            continue
        return urljoin(base_url, href)
    return None


def _feed_entries(feed_url: str, limit: int) -> list:
    """把 RSS/Atom 条目规整成和 HTML 抓取一致的结构。"""
    feed = feedparser.parse(feed_url)
    if getattr(feed, "bozo", 0) and not feed.entries:
        return []

    out = []
    for entry in feed.entries[:limit]:
        title = re.sub(r"\s+", " ", entry.get("title", "")).strip()
        link = entry.get("link", "")
        if not title or not link:
            continue
        out.append({
            "title": title,
            "link": link,
            "published": _rss_published_et(entry),
            "body": _entry_body(entry),
            "via": "RSS",
        })
    return out


def collect_company_articles(ticker: str, listing_url: str,
                             limit: int = MAX_WEB_CANDIDATES) -> list:
    """
    统一入口：先试 RSS，不行再退回爬列表页链接。
    返回 [{title, link, published, body, via}]，body 可能为空（后续再回源抓）。
    """
    feed_url = COMPANY_FEEDS.get(ticker)
    listing_html = None

    if not feed_url:
        try:
            listing_html = web_get(listing_url, timeout=30).text
            feed_url = discover_feed_url(listing_html, listing_url)
        except Exception as e:
            print(f"    ⚠️ 请求 {ticker} 官网失败: {e}")
            return []

    if feed_url:
        try:
            entries = _feed_entries(feed_url, limit)
            if entries:
                print(f"    📶 走 RSS: {feed_url}（{len(entries)} 条）")
                return entries
            print("    ℹ️ RSS 无有效条目，回退 HTML 抓取")
        except Exception as e:
            print(f"    ⚠️ RSS 解析失败，回退 HTML 抓取: {e}")

    if listing_html is None:
        try:
            listing_html = web_get(listing_url, timeout=30).text
        except Exception as e:
            print(f"    ⚠️ 请求 {ticker} 官网失败: {e}")
            return []

    links = scrape_listing_links(ticker, listing_url, listing_html, limit)
    if links:
        print(f"    🕸️ 走 HTML 抓取（{len(links)} 条候选）")
    return links


def scrape_listing_links(ticker: str, url: str, html: str,
                         limit: int = MAX_WEB_CANDIDATES) -> list:
    """只负责列出候选链接，正文由 fetch_article 单独抓。"""
    soup = BeautifulSoup(html, "lxml")

    cfg = NEWS_SELECTORS.get(ticker, NEWS_SELECTORS["default"])
    link_selector = cfg.get("link_selector", "a[href*='news'], a[href*='release']")
    title_selector = cfg.get("title_selector")
    exclude_patterns = cfg.get("exclude_patterns", [])

    listing_norm = url.rstrip("/").lower()
    candidates = []

    for a in soup.select(link_selector):
        href = a.get("href")
        if not href:
            continue
        if href.startswith("#") or href.startswith("javascript:") or href.startswith("mailto:"):
            continue
        if any(re.search(pat, href, re.I) for pat in exclude_patterns):
            continue

        if title_selector:
            title_elem = a.find_next(title_selector) or a
            title = title_elem.get_text(" ", strip=True)
        else:
            title = a.get_text(" ", strip=True)

        title = re.sub(r"\s+", " ", title).strip()
        if len(title) < 15 or _NAV_TITLES.match(title):
            continue

        href = urljoin(url, href).split("#")[0]
        if href.rstrip("/").lower() == listing_norm:
            continue
        if not href.startswith("http"):
            continue

        candidates.append({"title": title, "link": href,
                           "published": None, "body": "", "via": "HTML"})

    seen_links = set()
    unique = []
    for item in candidates:
        key = item["link"].rstrip("/")
        if key in seen_links:
            continue
        seen_links.add(key)
        unique.append(item)

    return unique[:limit]

# ==================== 文案生成 ====================

# 所有 prompt 共用的硬约束
FACT_RULES = """【最高优先级 · 事实约束，违反即作废】
- 你只能使用下方「原始材料」中出现过的信息。材料里没有的数字、金额、百分比、
  日期、人名、职位、产品型号、客户名称、因果关系，一律不得写入——
  即使你从训练数据或行业常识中知道，也不得补充。
- 不得把材料中的「预计 / 计划 / 拟」写成已经发生的事实。
- 不得把分析师观点、第三方推测写成公司官方说法。
- 材料不足以支撑一条完整快讯时，直接输出「材料不足」四个字，不要凑字数。
- 所有数字必须与原文逐位一致，不得换算、不得四舍五入、不得改单位。"""

# -------- SEC / 官网 通用自由格式 --------
SYSTEM_PROMPT = f"""你是 星火速报 的撰稿人，为小红书写美股与科技公司的新闻快讯。

{FACT_RULES}

写作要求：
1. 小红书风格：口语化、有网感，适当使用 emoji 做视觉分隔，但不夸张、不标题党。
2. 结构灵活，根据内容类型自由调整：可以是数据罗列、对话体、分段叙述等，不要用固定模板。
   材料里没有的段落直接删掉，不要为了结构完整而编内容。
3. 正文第一句交代美东时间（ET），格式如「美东时间7月23日」。
   如果材料未提供可确认的发布日期，则全文禁止出现任何具体日期。
4. 结尾单独一行注明来源，格式为「来源：[具体文件名称/机构名称]」。
5. 全文末尾带 5-8 个话题标签（以 # 开头）。
6. 全文 300-500 字。
7. 不要使用 markdown 标题符号（#、##、###）。
8. 语气平实、克制，像朋友聊天，避免生硬书面语。"""

# -------- TechCrunch 短格式 --------
TECHCRUNCH_PROMPT = f"""你是 星火速报 的撰稿人，为小红书写科技新闻短讯。

材料来自 TechCrunch，属于二手信源。

{FACT_RULES}

写作要求：
1. 用转述，不要整句照搬原文措辞。
2. 篇幅 120-200 字，不要为了凑长度而展开。
3. 日期时间一律用美东时间（ET）；材料没有明确日期就不写日期。
4. 格式：
   第一行：📡 [标题，20字以内]
   空行
   正文 2-3 段短句
   空行
   ─ 📌 来源：TechCrunch ─
   [3-5 个话题标签]"""


def generate_copy(content: str, company_name: str, ticker: str,
                  accepted_et, items: list, form: str,
                  source_url: str = "", custom_title: str = None,
                  is_routine: bool = False, date_known: bool = True) -> str:
    if not DEEPSEEK_API_KEY:
        return "【错误】未设置 DEEPSEEK_API_KEY 环境变量"

    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)

    item_desc = "、".join(
        f"{k} {WANTED_ITEMS.get(k, '其他')}" for k in items if k in WANTED_ITEMS
    ) if items else form

    if date_known and accepted_et is not None:
        time_line = f"发布/提交时间（美东）：{accepted_et.strftime('%Y-%m-%d %H:%M')} ET"
        date_rule = ""
    else:
        time_line = "发布时间：未能从原文确认"
        date_rule = ("\n⚠️ 材料中没有可确认的发布时间。全文禁止出现任何具体日期，"
                     "也不得用「今日」「昨日」等相对时间。若因此无法成文，输出「材料不足」。")

    title_hint = f"标题方向：{custom_title}" if custom_title else ""
    routine_hint = ""
    if is_routine:
        routine_hint = ("这是公司每月例行披露的高管持股变动或内部人交易，属于常规治理信息，"
                        "不是突发事件。请按「月度常规披露」的定位撰写，语气平和，不要过度解读。")

    user_prompt = f"""公司：{company_name}
股票代码：{ticker}
{time_line}
文件类型：{form}
涉及事项：{item_desc}
数据来源：{source_url if source_url else form}{date_rule}
{routine_hint}
{title_hint}

原始材料（以下是全部可用信息，此外的一切内容都不得使用）：
\"\"\"
{content}
\"\"\"

请按上述要求输出完整文案。"""

    system_prompt = TECHCRUNCH_PROMPT if form == "TechCrunch" else SYSTEM_PROMPT

    try:
        resp = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
            max_tokens=1200,
            timeout=90,
            extra_body={"thinking": {"type": "disabled"}},
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        return f"【生成失败】{e}"


_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
_DATE_LIKE = re.compile(
    r"\d{4}\s*[-/年]\s*\d{1,2}\s*[-/月]\s*\d{1,2}\s*日?"
    r"|\d{1,2}\s*[-/月]\s*\d{1,2}\s*日"
    r"|\d{1,2}:\d{2}"
    r"|Q[1-4]|[一二三四]季度|第[1-4一二三四]季度"
)


_SCALE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*[-\s]?(billion|million|trillion)", re.I)


def _fmt_num(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:g}"


def _derived_numbers(source_text: str) -> set:
    """
    英文数量级 → 中文数量级的换算白名单。
    原文写 "$500-billion"，文案写「5000亿美元」是对的，
    但纯子串比对会误报。这里预先把等价写法算出来。
    """
    out = set()
    for m in _SCALE.finditer(source_text):
        try:
            v = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        unit = m.group(2).lower()
        if unit == "billion":
            out.add(_fmt_num(v * 10))        # N billion = N*10 亿
            out.add(_fmt_num(v / 100))       # = N/100 万亿
        elif unit == "million":
            out.add(_fmt_num(v * 100))       # N million = N*100 万
            out.add(_fmt_num(v / 100))       # = N/100 亿
        elif unit == "trillion":
            out.add(_fmt_num(v * 10000))     # N trillion = N*10000 亿
            out.add(_fmt_num(v))             # = N 万亿
    return out


def check_numbers(copy_text: str, source_text: str, extra_allowed: str = "") -> list:
    def normalize(s):
        s = s.replace(",", "").replace(" ", "")
        s = re.sub(r"[€$¥]", "", s)
        s = re.sub(r"[亿万]欧元?", "", s)
        return s

    src = normalize(source_text + " " + extra_allowed)
    derived = _derived_numbers(source_text + " " + extra_allowed)
    cleaned = _DATE_LIKE.sub(" ", copy_text)

    suspicious = []
    for m in _NUM.finditer(cleaned):
        raw = m.group()
        val = normalize(raw)
        if len(val.replace(".", "")) <= 1:
            continue
        if re.fullmatch(r"(19|20)\d{2}", val):
            continue
        if val in src or val in derived:
            continue
        suspicious.append(raw)
    return sorted(set(suspicious))


def check_dates(copy_text: str, date_known: bool) -> list:
    """日期未知却写了具体日期 —— 这是最常见的一类幻觉，单独拦。"""
    if date_known:
        return []
    hits = re.findall(r"\d{1,2}\s*月\s*\d{1,2}\s*日|今日|昨日|当地时间\d", copy_text)
    if hits:
        return [f"发布时间未知但文案出现日期表述: {', '.join(sorted(set(hits))[:5])}"]
    return []


# 新闻稿 / SEC 申报里不可能出现的东西。出现即为模型自己补的。
# 「NVDA 盘后交易小幅波动」就是被这一条抓住的典型。
SUSPECT_PATTERNS = [
    (r"盘后|盘前|股价|市值|涨幅|跌幅|收涨|收跌|市场反应|投资者反应",
     "提及股价/盘后交易/市场反应 —— 官方新闻稿与申报文件不含这类信息"),
    (r"分析师(认为|预计|指出|表示)|机构(认为|预计)|华尔街(认为|预计)",
     "引用了分析师/机构观点 —— 材料里不会有"),
    (r"业内人士|知情人士|有消息称|据传|外界猜测",
     "出现无出处的传闻式表述"),
]


def check_grounding(copy_text: str) -> list:
    """生成后的兜底检查：抓那些结构上不可能来自材料的内容。"""
    out = []
    for pat, desc in SUSPECT_PATTERNS:
        m = re.search(pat, copy_text)
        if m:
            out.append(f"疑似编造（{desc}）：「{m.group()}」")
    return out

# ==================== SEC 处理流程 ====================


def process_company_sec(ticker: str, cik: int, seen: set):
    results = []
    filings = list_filings(cik, lookback_days=DAYS_LOOKBACK)

    if not filings:
        return results

    for f in filings:
        acc = f["accession"]
        if acc in seen:
            print(f"  ⏭️  已处理过: {acc}")
            continue

        decision, reason = should_process(f)
        is_routine_6k = (decision == "6-K_ROUTINE")

        if decision is False:
            print(f"  ⏭️  跳过 {acc} - {reason}")
            seen.add(acc)
            continue

        try:
            built = build_content(f)
        except Exception as e:
            print(f"  ❌ {acc} 内容提取失败: {e}")
            continue

        if len(built["text"]) < MIN_ARTICLE_CHARS:
            print(f"  📎 {acc} 材料过短（{len(built['text'])} 字符），只登记线索")
            results.append(_lead_record(
                ticker=ticker,
                company=COMPANIES.get(ticker, ticker),
                form=f["form"],
                dt=f["accepted_et"],
                rid=acc,
                url=f["primary_url"],
                items=f["items"] or ["材料过短"],
                source="SEC",
            ))
            seen.add(acc)
            continue

        if decision == "CHECK_5_02":
            low = built["text"].lower()
            if not any(k in low for k in EXEC_CHANGE_KEYWORDS):
                print(f"  ⏭️  跳过 {acc} - 5.02 常规董事会事项")
                seen.add(acc)
                continue
            print(f"  🔎 {acc} - 5.02 命中高管变动关键词")

        is_alert = bool(set(f["items"]) & ALERT_ITEMS)
        if is_alert:
            print(f"  🚨 高优先级事件: {','.join(set(f['items']) & ALERT_ITEMS)}")

        copy_text = generate_copy(
            content=built["text"],
            company_name=COMPANIES.get(ticker, ticker),
            ticker=ticker,
            accepted_et=f["accepted_et"],
            items=f["items"],
            form=f["form"],
            source_url=f["primary_url"],
            is_routine=is_routine_6k,
            date_known=True,
        )

        warnings = []
        if copy_text.startswith("【生成失败】") or copy_text.startswith("【错误】"):
            warnings.append("生成失败")
        elif copy_text.strip() == "材料不足":
            warnings.append("模型判定材料不足")
        else:
            bad = check_numbers(
                copy_text, built["text"],
                extra_allowed=f"{f['accepted_et'].strftime('%Y-%m-%d %H:%M')} {ticker}")
            if bad:
                warnings.append(f"数字未在原文中找到: {', '.join(bad)}")
                print(f"  ⚠️  数字校验告警: {', '.join(bad)}")

        if is_routine_6k:
            warnings.append("月度常规披露，非突发事件")

        results.append({
            "ticker": ticker,
            "company": COMPANIES.get(ticker, ticker),
            "form": f["form"],
            "accepted_et": f["accepted_et"],
            "accession": acc,
            "items": f["items"],
            "url": f["primary_url"],
            "exhibits": built["exhibits"],
            "copy": copy_text,
            "warnings": warnings,
            "alert": is_alert,
            "source": "SEC",
            "is_routine": is_routine_6k,
            "material_chars": len(built["text"]),
        })
        seen.add(acc)
        print(f"  ✅ {acc} 完成（材料 {len(built['text'])} 字符）")

    return results

# ==================== 线索记录 ====================


def _lead_record(ticker, company, form, dt, rid, url, items, source, note=""):
    return {
        "ticker": ticker,
        "company": company,
        "form": form,
        "accepted_et": dt or datetime.now(ET),
        "accession": rid,
        "items": items,
        "url": url,
        "exhibits": [],
        "copy": f"（仅登记线索，正文抓取不足以生成文案，请人工打开链接核实后手写。{note}）",
        "warnings": ["线索，未生成文案"],
        "alert": False,
        "source": source,
        "is_routine": False,
        "lead_only": True,
        "material_chars": 0,
    }

# ==================== TechCrunch 处理流程 ====================


def process_techcrunch(articles: list, seen: set) -> list:
    results = []

    for article in articles:
        aid = article["id"]
        if aid in seen:
            print(f"    ⏭️  已处理过: {article['title'][:50]}")
            continue

        published = article["published"]
        date_known = published is not None
        dt = published or datetime.now(ET)

        if article["thin"]:
            print(f"    📎 线索（正文过短，不生成文案）: {article['title'][:50]}")
            results.append(_lead_record(
                ticker=",".join(article["matched_companies"][:3]),
                company=", ".join(COMPANIES.get(t, t) for t in article["matched_companies"]),
                form="TechCrunch",
                dt=dt,
                rid=aid,
                url=article["link"],
                items=["TechCrunch 线索"],
                source="TechCrunch",
            ))
            seen.add(aid)
            continue

        content = (f"标题：{article['title']}\n"
                   f"原文链接：{article['link']}\n\n"
                   f"正文：\n{article['body']}")

        copy_text = generate_copy(
            content=content,
            company_name=", ".join(COMPANIES.get(t, t) for t in article["matched_companies"]),
            ticker=",".join(article["matched_companies"][:3]),
            accepted_et=dt,
            items=["TechCrunch 报道"],
            form="TechCrunch",
            source_url=article["link"],
            custom_title=article["title"].replace("|", " ").replace("\n", " ")[:100],
            is_routine=False,
            date_known=date_known,
        )

        warnings = ["二手信源，建议核对原始信源后再发布"]
        if copy_text.startswith("【生成失败】") or copy_text.startswith("【错误】"):
            warnings = ["生成失败"]
        elif copy_text.strip() == "材料不足":
            warnings = ["模型判定材料不足"]
        else:
            bad = check_numbers(
                copy_text, content,
                extra_allowed=",".join(article["matched_companies"]) +
                (f" {dt.strftime('%Y-%m-%d %H:%M')}" if date_known else ""))
            if bad:
                warnings.append(f"数字未在原文中找到: {', '.join(bad)}")
                print(f"    ⚠️  数字校验告警: {', '.join(bad)}")
            warnings.extend(check_dates(copy_text, date_known))
            if not date_known:
                warnings.append("发布时间未能确认")

        results.append({
            "ticker": ",".join(article["matched_companies"][:3]),
            "company": ", ".join(COMPANIES.get(t, t) for t in article["matched_companies"]),
            "form": "TechCrunch",
            "accepted_et": dt,
            "accession": aid,
            "items": ["TechCrunch 报道"],
            "url": article["link"],
            "exhibits": [],
            "copy": copy_text,
            "warnings": warnings,
            "alert": False,
            "source": "TechCrunch",
            "is_routine": False,
            "lead_only": False,
            "material_chars": len(article["body"]),
        })
        seen.add(aid)
        print(f"    ✅ {article['title'][:50]}（材料 {len(article['body'])} 字符）")

    return results

# ==================== 官网新闻处理流程 ====================


def process_company_website(ticker: str, url: str, seen: set) -> list:
    """
    这里是本次修改的重点。
    旧版：content = 标题 + 链接 → 模型只能编。
    新版：先 fetch_article 抓正文和发布日期，抓不到就不生成文案。
    """
    results = []
    articles = collect_company_articles(ticker, url, limit=MAX_WEB_CANDIDATES)
    if not articles:
        return results

    cutoff = datetime.now(ET) - timedelta(days=DAYS_LOOKBACK)

    for article in articles:
        aid = _stable_id("WEB", article["link"])
        if aid in seen:
            print(f"    ⏭️  已处理: {article['title'][:40]}")
            continue

        published = article.get("published")
        body = article.get("body") or ""
        title = article["title"]
        source_url = article["link"]
        truncated = False

        # RSS 已给足日期就先做窗口过滤，省掉一次回源请求
        if published and published < cutoff:
            print(f"    ⏭️  超出回溯窗口（{published.strftime('%Y-%m-%d')}）: {title[:40]}")
            seen.add(aid)
            continue

        # RSS 正文不够、或没给日期 → 回源抓原文
        if len(body) < MIN_ARTICLE_CHARS or published is None:
            try:
                art = fetch_article(article["link"])
                if len(art["text"]) > len(body):
                    body = art["text"]
                    truncated = art["truncated"]
                published = published or art["published"]
                title = art["title"] or title
                source_url = art["url"]
            except Exception as e:
                print(f"    ⚠️ 正文抓取失败（{title[:35]}）: {e}")
                if len(body) < MIN_ARTICLE_CHARS:
                    continue

        date_known = published is not None

        if date_known and published < cutoff:
            print(f"    ⏭️  超出回溯窗口（{published.strftime('%Y-%m-%d')}）: {title[:40]}")
            seen.add(aid)
            continue

        if len(body) < MIN_ARTICLE_CHARS:
            print(f"    📎 线索（正文仅 {len(body)} 字符）: {title[:40]}")
            results.append(_lead_record(
                ticker=ticker,
                company=COMPANIES.get(ticker, ticker),
                form="官网",
                dt=published,
                rid=aid,
                url=source_url,
                items=["官网新闻"],
                source="官网",
                note="可能是列表页、登录墙或 JS 渲染页面。",
            ))
            seen.add(aid)
            continue

        content = (f"标题：{title}\n"
                   f"来源页面：{source_url}\n"
                   + (f"原文发布时间：{published.strftime('%Y-%m-%d')} ET\n" if date_known else "")
                   + f"\n正文：\n{body}"
                   + ("\n\n（注：正文超长，已截断）" if truncated else ""))

        copy_text = generate_copy(
            content=content,
            company_name=COMPANIES.get(ticker, ticker),
            ticker=ticker,
            accepted_et=published,
            items=["官网新闻"],
            form="官网",
            source_url=source_url,
            custom_title=title[:100],
            is_routine=False,
            date_known=date_known,
        )

        warnings = []
        if not date_known:
            warnings.append("未能提取发布时间，发布前必须人工确认日期")
        if copy_text.startswith("【生成失败】") or copy_text.startswith("【错误】"):
            warnings.append("生成失败")
        elif copy_text.strip() == "材料不足":
            warnings.append("模型判定材料不足")
        else:
            bad = check_numbers(
                copy_text, content,
                extra_allowed=(published.strftime('%Y-%m-%d') if date_known else "") + f" {ticker}")
            if bad:
                warnings.append(f"数字未在原文中找到: {', '.join(bad)}")
                print(f"    ⚠️  数字校验告警: {', '.join(bad)}")
            warnings.extend(check_dates(copy_text, date_known))
            grounding = check_grounding(copy_text)
            warnings.extend(grounding)
            for g in grounding:
                print(f"    🚩 {g}")

        results.append({
            "ticker": ticker,
            "company": COMPANIES.get(ticker, ticker),
            "form": "官网",
            "accepted_et": published or datetime.now(ET),
            "accession": aid,
            "items": ["官网新闻"],
            "url": source_url,
            "exhibits": [],
            "copy": copy_text,
            "warnings": warnings,
            "alert": False,
            "source": "官网",
            "is_routine": False,
            "material_chars": len(body),
            "via": article.get("via", "HTML"),
        })
        seen.add(aid)
        print(f"    ✅ 官网新闻: {title[:40]}"
              f"（{article.get('via', 'HTML')}，正文 {len(body)} 字符）")

    return results

# ==================== 输出 ====================


# 从文件名里抠日期：summary_20260728_1240.md / 20260728-1240_MU_a1b2c3.md
_FNAME_DATE = re.compile(r"(20\d{6})[_-]\d{4}")


def _file_date(path: Path):
    """
    优先按文件名解析日期。
    绝不能只依赖 mtime：Actions 每次都是全新 checkout，
    git 会把所有文件的 mtime 设成签出那一刻，按 mtime 判断永远删不掉东西。
    """
    m = _FNAME_DATE.search(path.name)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y%m%d").replace(tzinfo=ET)
        except ValueError:
            pass
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=ET)
    except OSError:
        return None


def cleanup_outputs() -> tuple:
    """
    output/ 的保留策略：
    1. 只留 summary_*.md，其余（单条文案、leads）一律删除；
    2. summary 只留最近 OUTPUT_RETENTION_DAYS 个自然日。
    在写新内容之前跑，删掉的文件由 workflow 的 `git add -A` 带上删除记录。
    """
    if not OUTPUT_DIR.exists():
        return 0, 0

    cutoff_date = (datetime.now(ET) - timedelta(days=OUTPUT_RETENTION_DAYS)).date()
    dropped_non_summary = 0
    dropped_expired = 0

    for p in sorted(OUTPUT_DIR.rglob("*")):
        if p.is_dir() or p.name.startswith("."):
            continue
        if not p.name.startswith("summary_"):
            p.unlink(missing_ok=True)
            dropped_non_summary += 1
            continue
        d = _file_date(p)
        if d and d.date() < cutoff_date:
            p.unlink(missing_ok=True)
            dropped_expired += 1

    # 收掉空目录（比如 output/leads/）
    for p in sorted(OUTPUT_DIR.rglob("*"), key=lambda x: len(x.parts), reverse=True):
        if p.is_dir():
            try:
                p.rmdir()
            except OSError:
                pass

    return dropped_non_summary, dropped_expired


def write_outputs(all_results: list, checked: int):
    OUTPUT_DIR.mkdir(exist_ok=True)
    now_et = datetime.now(ET)

    if KEEP_INDIVIDUAL_FILES:
        for r in all_results:
            ts = r["accepted_et"].strftime("%Y%m%d-%H%M")
            safe_ticker = r["ticker"].replace(",", "-")[:20] or "NA"
            suffix = r["accession"][-6:]
            if r.get("lead_only"):
                path = OUTPUT_DIR / "leads" / f"{ts}_{safe_ticker}_{suffix}.md"
                path.parent.mkdir(parents=True, exist_ok=True)
            elif r["source"] == "TechCrunch":
                path = OUTPUT_DIR / f"TC_{ts}_{safe_ticker}_{suffix}.md"
            elif r["source"] == "官网":
                path = OUTPUT_DIR / f"WEB_{ts}_{safe_ticker}_{suffix}.md"
            else:
                path = OUTPUT_DIR / f"{ts}_{safe_ticker}_{suffix}.md"

            with path.open("w", encoding="utf-8") as fh:
                fh.write(f"# {r['company']}\n\n")
                fh.write(f"- 时间：{r['accepted_et'].strftime('%Y-%m-%d %H:%M')} ET\n")
                fh.write(f"- 类型：{r['form']}　事项：{', '.join(r['items']) or '—'}\n")
                fh.write(f"- 原文：{r['url']}\n")
                fh.write(f"- 材料长度：{r.get('material_chars', 0)} 字符\n")
                if r["exhibits"]:
                    for ex in r["exhibits"]:
                        fh.write(f"- 附件：{ex}\n")
                if r.get("is_routine"):
                    fh.write("- 📌 月度常规披露\n")
                if r["warnings"]:
                    fh.write(f"- ⚠️ 告警：{'；'.join(r['warnings'])}\n")
                fh.write("\n---\n\n")
                fh.write(r["copy"])

    # 汇总
    summary = OUTPUT_DIR / f"summary_{now_et.strftime('%Y%m%d_%H%M')}.md"
    with summary.open("w", encoding="utf-8") as fh:
        fh.write("# 监控汇总\n\n")
        fh.write(f"生成时间：{now_et.strftime('%Y-%m-%d %H:%M')} ET\n\n")
        drafts = [r for r in all_results if not r.get("lead_only")]
        leads = [r for r in all_results if r.get("lead_only")]

        fh.write(f"监控公司：{checked} 家　可用文案：{len(drafts)} 条\n\n")
        fh.write(f"- SEC 申报：{len([r for r in drafts if r['source'] == 'SEC'])} 条\n")
        fh.write(f"- TechCrunch：{len([r for r in drafts if r['source'] == 'TechCrunch'])} 条\n")
        fh.write(f"- 公司官网：{len([r for r in drafts if r['source'] == '官网'])} 条\n")
        if leads:
            fh.write(f"- 线索（未生成文案）：{len(leads)} 条\n")
        routine_count = len([r for r in all_results if r.get('is_routine', False)])
        if routine_count:
            fh.write(f"- 月度常规披露：{routine_count} 条\n")
        fh.write("\n")

        alerts = [r for r in drafts if r.get("alert", False)]
        if alerts:
            fh.write("## 🚨 高优先级\n\n")
            for r in alerts:
                fh.write(f"- {r['company']}　{', '.join(r['items'])}　{r['url']}\n")
            fh.write("\n")

        if leads:
            fh.write("## 📎 线索（正文抓取不足，需人工核实）\n\n")
            for r in leads:
                fh.write(f"- [{r['ticker']}] {r['url']}\n")
            fh.write("\n")

        fh.write("---\n\n")
        for i, r in enumerate(drafts, 1):
            routine_label = " 📌月度常规披露" if r.get('is_routine', False) else ""
            fh.write(f"## {i}. {r['company']}{routine_label}"
                     f"（{r['accepted_et'].strftime('%m-%d %H:%M')} ET）\n\n")
            fh.write(f"事项：{', '.join(r['items']) or r['form']}　"
                     f"类型：{r['form']}　材料：{r.get('material_chars', 0)} 字符\n\n")
            fh.write(f"原文：{r['url']}\n\n")
            for ex in r.get("exhibits") or []:
                fh.write(f"附件：{ex}\n\n")
            if r["warnings"]:
                fh.write(f"⚠️ {'；'.join(r['warnings'])}\n\n")
            fh.write(r["copy"] + "\n\n---\n\n")

    return summary

# ==================== 主流程 ====================


def main():
    now_et = datetime.now(ET)
    print(f"🚀 开始运行 - {now_et.strftime('%Y-%m-%d %H:%M:%S')} ET")
    print(f"📋 监控公司：{len(COMPANIES)} 家　回溯：{DAYS_LOOKBACK} 天")
    print(f"📏 正文门槛：{MIN_ARTICLE_CHARS} 字符（低于此值只登记线索，不调 API）")

    # ---- 0. 清理 ----
    dropped_non_summary, dropped_expired = cleanup_outputs()
    if dropped_non_summary or dropped_expired:
        print(f"🧹 output 清理：删除非 summary {dropped_non_summary} 个、"
              f"过期 summary {dropped_expired} 个（保留 {OUTPUT_RETENTION_DAYS} 天）")
    print("-" * 60)

    all_results = []
    failed_sec = []

    # ---- 1. SEC 监控 ----
    try:
        tmap = load_ticker_map()
    except Exception as e:
        print(f"❌ 无法加载 ticker→CIK 映射表：{e}")
        tmap = {}

    seen = load_seen()
    pruned = seen.prune(STATE_RETENTION_DAYS)
    if pruned:
        print(f"🧹 state 清理：裁掉 {pruned} 条 {STATE_RETENTION_DAYS} 天前的去重记录"
              f"（剩余 {len(seen)} 条）")
    save_seen(seen)

    for idx, ticker in enumerate(COMPANIES, 1):
        cik = tmap.get(ticker.upper())
        if not cik:
            print(f"[{idx}/{len(COMPANIES)}] ❓ {ticker} 未找到 CIK，跳过 SEC")
            continue
        print(f"[{idx}/{len(COMPANIES)}] 📡 SEC {ticker} ({COMPANIES[ticker]})")
        try:
            all_results.extend(process_company_sec(ticker, cik, seen))
        except Exception as e:
            print(f"  ❌ {ticker} SEC 整体失败：{e}")
            failed_sec.append(ticker)
        finally:
            save_seen(seen)

    # ---- 2. TechCrunch 监控 ----
    print("\n📡 正在抓取 TechCrunch 科技新闻...")
    try:
        tc_articles = fetch_techcrunch_news(limit=30)
        if tc_articles:
            print(f"  ✅ 找到 {len(tc_articles)} 篇与监控公司相关的文章")
            for article in tc_articles:
                tag = "线索" if article["thin"] else "可用"
                print(f"    📄 [{tag}] {article['title'][:55]} "
                      f"-> {', '.join(article['matched_companies'])}")
            all_results.extend(process_techcrunch(tc_articles, seen))
            save_seen(seen)
        else:
            print("  ℹ️ 暂无监控公司相关新闻")
    except Exception as e:
        print(f"  ❌ TechCrunch 抓取失败：{e}")

    # ---- 3. 公司官网监控 ----
    print("\n📡 正在抓取公司官网新闻...")
    web_total = 0
    web_started = time.monotonic()
    web_skipped = []
    for ticker, url in COMPANY_NEWS_URLS.items():
        if ticker not in COMPANIES:
            continue
        if time.monotonic() - web_started > WEB_PHASE_BUDGET_SEC:
            web_skipped.append(ticker)
            continue
        print(f"  📄 {ticker} ({COMPANIES[ticker]})")
        try:
            results = process_company_website(ticker, url, seen)
            if results:
                web_total += len(results)
                all_results.extend(results)
            else:
                print("    ℹ️ 无新增内容")
        except Exception as e:
            print(f"    ❌ 抓取失败: {e}")
        finally:
            save_seen(seen)
    print(f"  官网共产出 {web_total} 条记录")
    if web_skipped:
        print(f"  ⏱️ 超出时间预算，本轮跳过 {len(web_skipped)} 家："
              f"{', '.join(web_skipped)}（下轮会补上）")

    # ---- 4. 输出 ----
    print("-" * 60)
    if all_results:
        path = write_outputs(all_results, len(COMPANIES))
        drafts = [r for r in all_results if not r.get("lead_only")]
        sec_count = len([r for r in drafts if r['source'] == 'SEC'])
        tc_count = len([r for r in drafts if r['source'] == 'TechCrunch'])
        web_count = len([r for r in drafts if r['source'] == '官网'])
        lead_count = len(all_results) - len(drafts)
        routine_count = len([r for r in all_results if r.get('is_routine', False)])
        warned = [r for r in drafts if r["warnings"]]

        print(f"✅ 完成：产出 {len(drafts)} 条文案")
        print(f"   - SEC：{sec_count} 条")
        print(f"   - TechCrunch：{tc_count} 条")
        print(f"   - 公司官网：{web_count} 条")
        if lead_count:
            print(f"   - 线索（未生成文案）：{lead_count} 条")
        if routine_count:
            print(f"   - 月度常规披露：{routine_count} 条")
        if warned:
            print(f"⚠️  {len(warned)} 条带告警，发布前人工复核：")
            for r in warned:
                print(f"     {r['ticker']}: {'；'.join(r['warnings'])}")
        if failed_sec:
            print(f"❌ SEC 失败：{', '.join(failed_sec)}")
        print(f"📁 汇总：{path.absolute()}")
    else:
        print("📭 本次没有新的可用内容")


if __name__ == "__main__":
    main()
