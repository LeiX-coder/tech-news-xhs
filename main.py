#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
海外科技新闻监控 + 小红书文案生成
数据源：
1. SEC 8-K / 6-K 申报文件
2. TechCrunch 科技新闻（仅保留监控公司相关）
3. 公司官网新闻发布（Press Release）
"""

import os
import re
import json
import time
import hashlib
import threading
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from openai import OpenAI
import feedparser

# ==================== 配置区域 ====================

# SEC 强制要求：必须是真实的「名字 邮箱」
SEC_IDENTITY = "xiaolei xiaolei12555@126.com"

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

def sec_get(url: str, max_retries: int = 3, timeout: int = 45):
    for attempt in range(max_retries):
        with _lock:
            gap = time.monotonic() - _last_request[0]
            if gap < MIN_INTERVAL:
                time.sleep(MIN_INTERVAL - gap)
            _last_request[0] = time.monotonic()

        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
        except requests.RequestException as e:
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

def load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()

def save_seen(seen: set):
    STATE_DIR.mkdir(exist_ok=True)
    SEEN_FILE.write_text(
        json.dumps(sorted(seen), ensure_ascii=False, indent=0), encoding="utf-8"
    )

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
    items = filing["items"]
    parts = []

    body = clean_html(sec_get(filing["primary_url"]).text)
    if len(body) < 100:
        raise RuntimeError(f"正文提取过短（{len(body)} 字符），疑似解析失败")
    parts.append(f"【8-K 正文】\n{body[:6000]}")

    exhibits_used = []
    docs = None

    if "2.02" in items or filing["form"] == "6-K":
        docs = list_documents(filing["base"], filing["accession"])
        ex = find_exhibit(docs, 99, 1) or find_exhibit(docs, 99, 0)
        if ex:
            pr = clean_html(sec_get(ex["url"]).text)
            parts.append(
                f"\n【新闻稿原文 {ex['type']}】\n{extract_press_release(pr)}"
            )
            exhibits_used.append(ex["url"])
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

MIN_SUMMARY_CHARS = 180

def fetch_techcrunch_news(limit: int = 30):
    try:
        feed = feedparser.parse(TECHCRUNCH_RSS_URL)
    except Exception as e:
        print(f"  ⚠️ TechCrunch RSS 抓取失败: {e}")
        return []

    if getattr(feed, "bozo", 0) and not feed.entries:
        print(f"  ⚠️ RSS 解析异常: {getattr(feed, 'bozo_exception', '未知')}")
        return []

    out = []
    for entry in feed.entries[:limit]:
        title = entry.get("title", "")
        summary = clean_html(entry.get("summary", ""))
        matched = extract_matched_companies(f"{title} {summary}")
        if not matched:
            continue
        out.append({
            "id": _stable_id("TC", entry.get("link", title)),
            "title": title,
            "link": entry.get("link", ""),
            "summary": summary,
            "published": entry.get("published", ""),
            "source": "TechCrunch",
            "matched_companies": matched,
            "thin": len(summary) < MIN_SUMMARY_CHARS,
        })
    return out

# ==================== 官网新闻抓取 ====================

def fetch_company_news(ticker: str, url: str, limit: int = 5) -> list:
    try:
        html = sec_get(url, timeout=30).text
        soup = BeautifulSoup(html, "lxml")
    except Exception as e:
        print(f"    ⚠️ 请求 {ticker} 官网失败: {e}")
        return []

    cfg = NEWS_SELECTORS.get(ticker, NEWS_SELECTORS["default"])
    link_selector = cfg.get("link_selector", "a[href*='news'], a[href*='release']")
    title_selector = cfg.get("title_selector")
    exclude_patterns = cfg.get("exclude_patterns", [])

    candidates = []
    for a in soup.select(link_selector):
        href = a.get("href")
        if not href:
            continue
        if href.startswith("#") or href.startswith("javascript:"):
            continue
        if any(re.search(pat, href, re.I) for pat in exclude_patterns):
            continue

        if title_selector:
            title_elem = a.find_next(title_selector) or a
            title = title_elem.get_text(strip=True)
        else:
            title = a.get_text(strip=True)

        if len(title) < 10:
            continue

        if href.startswith("/"):
            base_url = re.match(r'(https?://[^/]+)', url)
            if base_url:
                href = base_url.group(1) + href
        elif not href.startswith("http"):
            href = urljoin(url, href)

        candidates.append({
            "title": title,
            "link": href,
        })

    seen_titles = set()
    unique = []
    for item in candidates:
        if item["title"] not in seen_titles:
            seen_titles.add(item["title"])
            unique.append(item)

    return unique[:limit]

# ==================== 文案生成 ====================

# -------- SEC / 官网 通用自由格式 --------
SYSTEM_PROMPT = """你是 星火速报 的撰稿人，为小红书写美股与科技公司的新闻快讯。

要求：
1. 小红书风格：口语化、有网感，适当使用 emoji 做视觉分隔，但不夸张、不标题党。
2. 内容必须基于材料事实，不编造任何数字、时间、人名、因果推断。
3. 结构灵活，根据内容类型自由调整：可以是数据罗列、对话体、分段叙述等，不要用固定模板。
4. 正文第一句必须交代美东时间（ET），格式如「美东时间7月23日」。
5. 结尾单独一行注明来源，格式为「来源：[具体文件名称/机构名称]」。
6. 全文末尾带 5-8 个话题标签（以 # 开头）。
7. 全文 300-500 字。
8. 如果材料信息不足以支撑一条完整快讯，直接输出「材料不足」四个字，不要勉强凑字数。
9. 不要使用 markdown 标题符号（#、##、###）。
10. 语气平实、克制，像朋友聊天，避免生硬书面语。
11. 数字必须与原文完全一致。"""

# -------- TechCrunch 短格式 --------
TECHCRUNCH_PROMPT = """你是 星火速报 的撰稿人，为小红书写科技新闻短讯。

材料来自 TechCrunch 的 RSS 摘要，信息量有限。严格遵守：

1. 只写材料中明确出现的内容。材料没提到的数字、金额、时间、人名、
   产品参数、因果关系，一律不得补充——哪怕你知道相关背景也不行。
2. 用转述，不要整句照搬原文措辞。
3. 篇幅 120-200 字，不要为了凑长度而展开。
4. 日期时间一律用美东时间（ET）。
5. 格式：
   第一行：📡 [标题，20字以内]
   空行
   正文 2-3 段短句
   空行
   ─ 📌 来源：TechCrunch ─
   [3-5 个话题标签]
6. 如果材料只够写一个标题、无法支撑一条独立短讯，直接输出「材料不足」。"""

def generate_copy(content: str, company_name: str, ticker: str,
                  accepted_et: datetime, items: list, form: str,
                  source_url: str = "", custom_title: str = None,
                  is_routine: bool = False) -> str:
    if not DEEPSEEK_API_KEY:
        return "【错误】未设置 DEEPSEEK_API_KEY 环境变量"

    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)

    item_desc = "、".join(
        f"{k} {WANTED_ITEMS.get(k, '其他')}" for k in items if k in WANTED_ITEMS
    ) if items else form

    title_hint = f"标题方向：{custom_title}" if custom_title else ""
    routine_hint = ""
    if is_routine:
        routine_hint = "这是公司每月例行披露的高管持股变动或内部人交易，属于常规治理信息，不是突发事件。请按「月度常规披露」的定位撰写，语气平和，不要过度解读。"

    user_prompt = f"""公司：{company_name}
股票代码：{ticker}
提交时间（美东）：{accepted_et.strftime('%Y-%m-%d %H:%M')} ET
文件类型：{form}
涉及事项：{item_desc}
数据来源：{source_url if source_url else form}
{routine_hint}
{title_hint}

原始材料：
{content}

请按上述要求输出完整文案。"""

    system_prompt = TECHCRUNCH_PROMPT if form == "TechCrunch" else SYSTEM_PROMPT

    try:
        resp = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
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

def check_numbers(copy_text: str, source_text: str, extra_allowed: str = "") -> list:
    def normalize(s):
        s = s.replace(",", "").replace(" ", "")
        s = re.sub(r"[€$¥]", "", s)
        s = re.sub(r"[亿万]欧元?", "", s)
        return s

    src = normalize(source_text + " " + extra_allowed)
    cleaned = _DATE_LIKE.sub(" ", copy_text)

    suspicious = []
    for m in _NUM.finditer(cleaned):
        raw = m.group()
        val = normalize(raw)
        if len(val.replace(".", "")) <= 1:
            continue
        if re.fullmatch(r"(19|20)\d{2}", val):
            continue
        if val not in src:
            suspicious.append(raw)
    return sorted(set(suspicious))

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
            is_routine=is_routine_6k
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
            "is_routine": is_routine_6k
        })
        seen.add(acc)
        print(f"  ✅ {acc} 完成（材料 {len(built['text'])} 字符）")

    return results

# ==================== TechCrunch 处理流程 ====================

def process_techcrunch(articles: list, seen: set) -> list:
    results = []

    for article in articles:
        aid = article["id"]
        if aid in seen:
            print(f"    ⏭️  已处理过: {article['title'][:50]}")
            continue

        if article["thin"]:
            print(f"    📎 线索（摘要过短，不生成文案）: {article['title'][:50]}")
            results.append({
                "ticker": ",".join(article["matched_companies"][:3]),
                "company": ", ".join(COMPANIES.get(t, t) for t in article["matched_companies"]),
                "form": "TechCrunch",
                "accepted_et": datetime.now(ET),
                "accession": aid,
                "items": ["TechCrunch 线索"],
                "url": article["link"],
                "exhibits": [],
                "copy": "（仅登记线索，摘要信息不足以生成文案。建议顺着链接找原始信源后手写。）",
                "warnings": ["线索，未生成文案"],
                "alert": False,
                "source": "TechCrunch",
                "is_routine": False,
                "lead_only": True,
            })
            seen.add(aid)
            continue

        content = f"标题：{article['title']}\n\n摘要：{article['summary']}\n\n原文链接：{article['link']}"

        copy_text = generate_copy(
            content=content,
            company_name=", ".join(COMPANIES.get(t, t) for t in article["matched_companies"]),
            ticker=",".join(article["matched_companies"][:3]),
            accepted_et=datetime.now(ET),
            items=["TechCrunch 报道"],
            form="TechCrunch",
            source_url=article["link"],
            custom_title=article["title"].replace("|", " ").replace("\n", " ")[:100],
            is_routine=False,
        )

        warnings = ["二手信源，建议核对原始信源后再发布"]
        if copy_text.startswith("【生成失败】") or copy_text.startswith("【错误】"):
            warnings = ["生成失败"]
        elif copy_text.strip() == "材料不足":
            warnings = ["模型判定材料不足"]
        else:
            bad = check_numbers(
                copy_text, content,
                extra_allowed=",".join(article["matched_companies"]))
            if bad:
                warnings.append(f"数字未在原文中找到: {', '.join(bad)}")
                print(f"    ⚠️  数字校验告警: {', '.join(bad)}")

        results.append({
            "ticker": ",".join(article["matched_companies"][:3]),
            "company": ", ".join(COMPANIES.get(t, t) for t in article["matched_companies"]),
            "form": "TechCrunch",
            "accepted_et": datetime.now(ET),
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
        })
        seen.add(aid)
        print(f"    ✅ {article['title'][:50]}")

    return results

# ==================== 官网新闻处理流程 ====================

def process_company_website(ticker: str, url: str, seen: set) -> list:
    results = []
    articles = fetch_company_news(ticker, url, limit=5)
    if not articles:
        return results

    print(f"    官网抓取到 {len(articles)} 篇新闻")
    for article in articles:
        aid = _stable_id("WEB", article["link"])
        if aid in seen:
            print(f"    ⏭️  已处理: {article['title'][:40]}")
            continue

        content = f"标题：{article['title']}\n\n原文链接：{article['link']}"
        copy_text = generate_copy(
            content=content,
            company_name=COMPANIES.get(ticker, ticker),
            ticker=ticker,
            accepted_et=datetime.now(ET),
            items=["官网新闻"],
            form="官网",
            source_url=article["link"],
            custom_title=article["title"][:100],
            is_routine=False,
        )

        warnings = []
        if copy_text.startswith("【生成失败】") or copy_text.startswith("【错误】"):
            warnings.append("生成失败")
        elif copy_text.strip() == "材料不足":
            warnings.append("模型判定材料不足（仅标题，需补充正文）")
        else:
            # 官网新闻只有标题和链接，无法做完整数字校验，暂时跳过
            pass

        results.append({
            "ticker": ticker,
            "company": COMPANIES.get(ticker, ticker),
            "form": "官网",
            "accepted_et": datetime.now(ET),
            "accession": aid,
            "items": ["官网新闻"],
            "url": article["link"],
            "exhibits": [],
            "copy": copy_text,
            "warnings": warnings,
            "alert": False,
            "source": "官网",
            "is_routine": False,
        })
        seen.add(aid)
        print(f"    ✅ 官网新闻: {article['title'][:40]}")

    return results

# ==================== 输出 ====================

def write_outputs(all_results: list, checked: int):
    OUTPUT_DIR.mkdir(exist_ok=True)
    now_et = datetime.now(ET)

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
            fh.write(f"- 提交时间：{r['accepted_et'].strftime('%Y-%m-%d %H:%M')} ET\n")
            fh.write(f"- 类型：{r['form']}　事项：{', '.join(r['items']) or '—'}\n")
            fh.write(f"- 原文：{r['url']}\n")
            if r["exhibits"]:
                for ex in r["exhibits"]:
                    fh.write(f"- 附件：{ex}\n")
            if r.get("is_routine"):
                fh.write(f"- 📌 月度常规披露\n")
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
            fh.write(f"- TechCrunch 线索（未生成文案）：{len(leads)} 条\n")
        routine_count = len([r for r in all_results if r.get('is_routine', False)])
        if routine_count:
            fh.write(f"- 月度常规披露：{routine_count} 条\n\n")

        alerts = [r for r in drafts if r.get("alert", False)]
        if alerts:
            fh.write("## 🚨 高优先级\n\n")
            for r in alerts:
                fh.write(f"- {r['company']}　{', '.join(r['items'])}　{r['url']}\n")
            fh.write("\n")

        if leads:
            fh.write("## 📎 线索（信息不足，需自行找原始信源）\n\n")
            for r in leads:
                fh.write(f"- [{r['ticker']}] {r['url']}\n")
            fh.write("\n")

        fh.write("---\n\n")
        for i, r in enumerate(drafts, 1):
            routine_label = " 📌月度常规披露" if r.get('is_routine', False) else ""
            fh.write(f"## {i}. {r['company']}{routine_label}（{r['accepted_et'].strftime('%m-%d %H:%M')} ET）\n\n")
            fh.write(f"事项：{', '.join(r['items']) or r['form']}\n\n")
            fh.write(f"原文：{r['url']}\n\n")
            if r["warnings"]:
                fh.write(f"⚠️ {'；'.join(r['warnings'])}\n\n")
            fh.write(r["copy"] + "\n\n---\n\n")

    return summary

# ==================== 主流程 ====================

def main():
    now_et = datetime.now(ET)
    print(f"🚀 开始运行 - {now_et.strftime('%Y-%m-%d %H:%M:%S')} ET")
    print(f"📋 监控公司：{len(COMPANIES)} 家　回溯：{DAYS_LOOKBACK} 天")
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
                print(f"    📄 [{tag}] {article['title'][:55]} -> {', '.join(article['matched_companies'])}")
            all_results.extend(process_techcrunch(tc_articles, seen))
            save_seen(seen)
        else:
            print("  ℹ️ 暂无监控公司相关新闻")
    except Exception as e:
        print(f"  ❌ TechCrunch 抓取失败：{e}")

    # ---- 3. 公司官网监控 ----
    print("\n📡 正在抓取公司官网新闻...")
    web_count = 0
    for ticker, url in COMPANY_NEWS_URLS.items():
        if ticker not in COMPANIES:
            continue
        print(f"  📄 {ticker} ({COMPANIES[ticker]})")
        try:
            results = process_company_website(ticker, url, seen)
            if results:
                web_count += len(results)
                all_results.extend(results)
            else:
                print("    ℹ️ 无新闻或抓取失败")
        except Exception as e:
            print(f"    ❌ 抓取失败: {e}")
    print(f"  官网共抓取 {web_count} 篇新闻")

    # ---- 4. 输出 ----
    print("-" * 60)
    if all_results:
        path = write_outputs(all_results, len(COMPANIES))
        sec_count = len([r for r in all_results if r['source'] == 'SEC'])
        tc_count = len([r for r in all_results if r['source'] == 'TechCrunch' and not r.get('lead_only')])
        web_count = len([r for r in all_results if r['source'] == '官网'])
        lead_count = len([r for r in all_results if r.get('lead_only')])
        routine_count = len([r for r in all_results if r.get('is_routine', False)])
        warned = [r for r in all_results if r["warnings"]]

        print(f"✅ 完成：产出 {len(all_results) - lead_count} 条文案")
        print(f"   - SEC：{sec_count} 条")
        print(f"   - TechCrunch：{tc_count} 条")
        print(f"   - 公司官网：{web_count} 条")
        if lead_count:
            print(f"   - TechCrunch 线索（未生成文案）：{lead_count} 条")
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
