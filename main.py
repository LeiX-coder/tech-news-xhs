#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
海外科技新闻监控 + 小红书文案生成
数据源：
1. SEC 8-K / 6-K 申报文件
2. TechCrunch 科技新闻（仅保留监控公司相关）
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

import requests
from bs4 import BeautifulSoup
from openai import OpenAI
import feedparser

# ==================== 配置区域 ====================

# SEC 强制要求：必须是真实的「名字 邮箱」
SEC_IDENTITY = "xiaolei xiaolei12555@126.com"

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

DAYS_LOOKBACK = 3

# 白名单：只处理这些 Item
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

# 附属/程序性条目，单独出现时不构成新闻
FILLER_ITEMS = {"7.01", "8.01", "9.01", "5.03", "5.07", "5.08"}

# 最高优先级：出现即单独告警
ALERT_ITEMS = {"4.02", "1.03", "3.01"}

# 5.02 关键词二次筛选
EXEC_CHANGE_KEYWORDS = [
    "resign", "resignation", "step down", "stepping down", "depart",
    "terminate", "termination", "effective immediately", "transition",
    "chief executive", "chief financial", "chief operating",
    "interim", "successor", "retire", "retirement",
]

# TechCrunch RSS
TECHCRUNCH_RSS_URL = "https://techcrunch.com/feed/"

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-v4-flash"

ET = ZoneInfo("America/New_York")
SEC = "https://www.sec.gov"

STATE_DIR = Path("state")
OUTPUT_DIR = Path("output")
SEEN_FILE = STATE_DIR / "seen_accessions.json"

# ==================== HTTP 层：限速 + 重试 ====================

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
            # 6-K 没有 Item 代码，这个字段是判断它讲什么的唯一结构化线索
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
        # 6-K 没有 Item 代码。primaryDocDescription 是 submissions API 里
        # 唯一说明这份文件讲什么的结构化字段——比在 URL 文件名里
        # 找关键词靠谱得多（文件名通常只是 form6k.htm）。
        haystack = f"{filing.get('doc_desc', '')} {filing.get('primary_url', '')}".lower()

        ROUTINE_KEYWORDS = [
            "month end", "monthend", "monthly",     # 月度持股/股本变动
            "insider", "shareholding", "share holding",
            "beneficial", "exempt",
            "total voting rights", "share capital",
            "transaction in own shares", "buy-back", "buyback",
            "director", "pdmr",                     # 董监高交易申报
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


# ==================== TechCrunch RSS 抓取 ====================

# 公司别名表。
# 不能从 COMPANIES 自动生成：那里的值是「英伟达 Nvidia」这种中英混排，
# 拿去匹配英文原文永远不中。也不能拿 ticker 当关键词——MU / ZS / STX
# 这类两字母代码做子串匹配会命中 community、museum、must 之类的普通单词。
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

# 预编译：整词匹配，避免 "amd" 命中 "amds"、"meta" 命中 "metadata"
_ALIAS_PATTERNS = {
    ticker: [re.compile(r"(?<![a-z0-9])" + re.escape(a) + r"(?![a-z0-9])", re.I)
             for a in names]
    for ticker, names in ALIASES.items()
}


def extract_matched_companies(text: str) -> list:
    """返回文本中确实提到的监控公司 ticker 列表。"""
    matched = []
    for ticker, pats in _ALIAS_PATTERNS.items():
        if any(p.search(text) for p in pats):
            matched.append(ticker)
    return matched


def _stable_id(prefix: str, key: str) -> str:
    """稳定去重键。

    注意不能用内置 hash()：Python 对字符串的 hash 每个进程都随机化，
    同一条链接每次运行得到的值都不一样，去重会完全失效。
    """
    return f"{prefix}_{hashlib.sha1(key.encode('utf-8')).hexdigest()[:16]}"


# 摘要短于这个长度就只当线索登记，不生成文案——
# 光靠一个标题写不出 300 字的准确快讯，只会逼模型编。
MIN_SUMMARY_CHARS = 180


def fetch_techcrunch_news(limit: int = 30):
    """从 TechCrunch RSS 抓取文章，只保留监控公司相关的。"""
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


# ==================== 文案生成 ====================

SYSTEM_PROMPT = """你是 星火速报 的撰稿人，为小红书写美股与科技公司的新闻快讯。

严格按以下格式输出，这是固定模板，必须遵守：

第一行：📡 [核心消息标题]，20字以内，以 emoji 开头
第二行：空行
第三行：📅 [日期]，[公司全名]（[股票代码]）发布[公告类型]
        日期与时间一律使用美东时间（ET），全文不得出现北京时间或其他时区。
第四行：空行
第五行：一句话总括，带 emoji 和 👇
第六行：空行

正文分四个段落，每段用 emoji 分隔符开头：

─── 🚀 [段落一标题] ───
💥 [核心要点]：[数据]
📈 [次要要点]：[数据]
· [子要点]

─── 📊 [段落二标题] ───
💵 [财务数据要点]
📉 [变化说明]
（若材料中没有财务数据，整段省略，不要编造数字来填满模板）

─── 💡 [段落三标题] ───
[管理层解读或影响分析]
注意：必须用转述方式，不要直接引用原话。

─── 📝 总结 ───
[一句话总结]

最后两行：
─ 📌 来源：[具体文件名称] ─

[5-8个话题标签，以 # 开头，空格分隔]

文风规范：
1. 语气平实、克制，像朋友聊天，不夸张、不煽动
2. 用口语化表达，避免生硬书面语
3. 所有数字必须与原文完全一致，材料里没有的数字一律不得出现
4. 段落是可选的：材料支撑不了某一段就整段删掉，模板服从事实，不是反过来
5. 不使用 markdown 标题符号
6. 全文 300-500 字
7. 如果材料信息不足以支撑一条完整快讯，直接输出「材料不足」"""


# TechCrunch 只有标题 + 一小段摘要，撑不起上面那套四段模板。
# 单独一套短格式，并且明确要求「不要补充材料之外的任何信息」。
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
                  custom_title: str = None, is_routine: bool = False) -> str:
    if not DEEPSEEK_API_KEY:
        return "【错误】未设置 DEEPSEEK_API_KEY 环境变量"

    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)

    item_desc = "、".join(
        f"{k} {WANTED_ITEMS.get(k, '其他')}" for k in items if k in WANTED_ITEMS
    ) if items else form

    title_hint = f"标题方向：{custom_title}" if custom_title else ""
    
    # 如果是月度常规披露，在提示中说明
    routine_hint = ""
    if is_routine:
        routine_hint = "这是公司每月例行披露的高管持股变动或内部人交易，属于常规治理信息，不是突发事件。请按「月度常规披露」的定位撰写，语气平和，不要过度解读。"

    user_prompt = f"""公司：{company_name}
股票代码：{ticker}
提交时间（美东）：{accepted_et.strftime('%Y-%m-%d %H:%M')} ET
文件类型：{form}
涉及事项：{item_desc}
{routine_hint}
{title_hint}

原始材料：
{content}

请按上述格式输出完整文案。"""

    try:
        resp = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[
                {"role": "system",
                 "content": TECHCRUNCH_PROMPT if form == "TechCrunch" else SYSTEM_PROMPT},
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

# 日期和时间会被拆成一串数字（2026-07-26 -> 2026 / 07 / 26），
# 而这些数字来自元数据、不在原文里，不排除掉的话每条文案都会误报。
_DATE_LIKE = re.compile(
    r"\d{4}\s*[-/年]\s*\d{1,2}\s*[-/月]\s*\d{1,2}\s*日?"   # 2026-07-26 / 2026年7月26日
    r"|\d{1,2}\s*[-/月]\s*\d{1,2}\s*日"                       # 7月26日
    r"|\d{1,2}:\d{2}"                                          # 16:05
    r"|Q[1-4]|[一二三四]季度|第[1-4一二三四]季度"
)


def check_numbers(copy_text: str, source_text: str, extra_allowed: str = "") -> list:
    """校验文案里的数字是否都能在原始材料中找到。

    extra_allowed 用来放元数据里合法带入的内容（提交时间、股票代码等）。
    """
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
        if re.fullmatch(r"(19|20)\d{2}", val):      # 年份
            continue
        if val not in src:
            suspicious.append(raw)
    return sorted(set(suspicious))


# ==================== SEC 处理流程 ====================

def process_company(ticker: str, cik: int, seen: set):
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
        
        # 判断是否为月度常规 6-K
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

        # 生成文案，传入 is_routine 参数
        copy_text = generate_copy(
            built["text"],
            COMPANIES.get(ticker, ticker),
            ticker,
            f["accepted_et"],
            f["items"],
            f["form"],
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

        # 如果是 routine 6-K，添加标记
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
    """处理 TechCrunch 文章。

    和 SEC 那条路径共用同一个 seen 集合与同一套数字校验——
    之前这两样在 TechCrunch 上都是绕过的。
    """
    results = []

    for article in articles:
        aid = article["id"]
        if aid in seen:
            print(f"    ⏭️  已处理过: {article['title'][:50]}")
            continue

        # 摘要太短就只登记线索，不烧 API，也不给模型编造的机会
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

        content = (f"标题：{article['title']}\n\n"
                   f"摘要：{article['summary']}\n\n"
                   f"原文链接：{article['link']}")

        copy_text = generate_copy(
            content=content,
            company_name=", ".join(COMPANIES.get(t, t) for t in article["matched_companies"]),
            ticker=",".join(article["matched_companies"][:3]),
            accepted_et=datetime.now(ET),
            items=["TechCrunch 报道"],
            form="TechCrunch",
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
    failed = []

    # ===== SEC 监控 =====
    try:
        tmap = load_ticker_map()
    except Exception as e:
        print(f"❌ 无法加载 ticker→CIK 映射表：{e}")
        return

    seen = load_seen()

    for idx, ticker in enumerate(COMPANIES, 1):
        cik = tmap.get(ticker.upper())
        if not cik:
            print(f"[{idx}/{len(COMPANIES)}] ❓ {ticker} 未找到 CIK，跳过")
            failed.append(ticker)
            continue

        print(f"[{idx}/{len(COMPANIES)}] 📡 {ticker} ({COMPANIES[ticker]})")
        try:
            all_results.extend(process_company(ticker, cik, seen))
        except Exception as e:
            print(f"  ❌ {ticker} 整体失败：{e}")
            failed.append(ticker)
        finally:
            save_seen(seen)

    # ===== TechCrunch 监控 =====
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

    # ===== 输出 =====
    print("-" * 60)
    if all_results:
        path = write_outputs(all_results, len(COMPANIES))
        sec_count = len([r for r in all_results if r['source'] == 'SEC'])
        tc_count = len([r for r in all_results
                        if r['source'] == 'TechCrunch' and not r.get('lead_only')])
        lead_count = len([r for r in all_results if r.get('lead_only')])
        routine_count = len([r for r in all_results if r.get('is_routine', False)])
        warned = [r for r in all_results if r["warnings"]]

        print(f"✅ 完成：产出 {len(all_results) - lead_count} 条文案")
        print(f"   - SEC：{sec_count} 条")
        print(f"   - TechCrunch：{tc_count} 条")
        if lead_count:
            print(f"   - TechCrunch 线索（未生成文案）：{lead_count} 条")
        if routine_count:
            print(f"   - 月度常规披露：{routine_count} 条")
        if warned:
            print(f"⚠️  {len(warned)} 条带告警，发布前人工复核：")
            for r in warned:
                print(f"     {r['ticker']}: {'；'.join(r['warnings'])}")
        if failed:
            print(f"❌ {len(failed)} 家公司抓取失败：{', '.join(failed)}")
        print(f"📁 汇总：{path.absolute()}")
    else:
        print("📭 本次没有新的可用内容")
        if failed:
            print(f"❌ 抓取失败：{', '.join(failed)}")


if __name__ == "__main__":
    main()
