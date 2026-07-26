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
        # 6-K 全部保留，但标记类型供后续处理参考
        url = filing.get("primary_url", "")
        is_routine = False
        routine_reason = ""

        ROUTINE_KEYWORDS = [
            "monthend",      # 月度持股变动
            "insider",       # 内部人交易
            "shareholding",  # 持股变动
            "exempt",        # 豁免申报
            "beneficial",    # 受益所有权变动
        ]

        for kw in ROUTINE_KEYWORDS:
            if kw in url.lower():
                is_routine = True
                routine_reason = kw
                break

        if is_routine:
            return "6-K_ROUTINE", f"6-K 琐碎事项（{routine_reason}，但保留）"
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

# 从 COMPANIES 自动生成搜索关键词
_COMPANY_SEARCH_TERMS = set()
for ticker, name in COMPANIES.items():
    _COMPANY_SEARCH_TERMS.add(ticker.upper())
    _COMPANY_SEARCH_TERMS.add(ticker.lower())
    # 提取核心名称（去掉 " Inc."、" Corporation" 等）
    core = re.sub(r'\s+(Inc\.?|Corp\.?|Corporation|LLC|Ltd\.?|Limited|公司)$', '', name, flags=re.I)
    _COMPANY_SEARCH_TERMS.add(core)
    # 如果名称包含括号，也提取括号外的部分
    if '(' in name:
        main_name = name.split('(')[0].strip()
        _COMPANY_SEARCH_TERMS.add(main_name)
    _COMPANY_SEARCH_TERMS.add(name)


def is_company_mentioned(text: str) -> bool:
    """检查文本中是否提到监控名单中的公司"""
    text_lower = text.lower()
    for term in _COMPANY_SEARCH_TERMS:
        if term and term.lower() in text_lower:
            return True
    return False


def fetch_techcrunch_news(limit: int = 30):
    """从 TechCrunch RSS 抓取文章，只保留监控公司相关的"""
    try:
        feed = feedparser.parse(TECHCRUNCH_RSS_URL)
        
        filtered = []
        for entry in feed.entries[:limit]:
            full_text = entry.title + " " + entry.summary
            if is_company_mentioned(full_text):
                # 找出具体提到了哪家公司
                matched = []
                for ticker, name in COMPANIES.items():
                    if ticker.upper() in full_text.upper() or name in full_text:
                        if ticker not in matched:
                            matched.append(ticker)
                
                filtered.append({
                    "title": entry.title,
                    "link": entry.link,
                    "summary": entry.summary,
                    "published": entry.get("published", ""),
                    "source": "TechCrunch",
                    "matched_companies": matched
                })
        
        return filtered
    except Exception as e:
        print(f"  ⚠️ TechCrunch RSS 抓取失败: {e}")
        return []


# ==================== 文案生成 ====================

SYSTEM_PROMPT = """你是 星火速报 的撰稿人，为小红书写美股与科技公司的新闻快讯。

严格按以下格式输出，这是固定模板，必须遵守：

第一行：📡 [核心消息标题]，20字以内，以 emoji 开头
第二行：空行
第三行：📅 [日期]，[公司全名]（[股票代码]）发布[公告类型]
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
3. 所有数字必须与原文完全一致
4. 不使用 markdown 标题符号
5. 全文 300-500 字
6. 如果材料信息不足以支撑一条完整快讯，直接输出「材料不足」"""


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
                {"role": "system", "content": SYSTEM_PROMPT},
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


def check_numbers(copy_text: str, source_text: str) -> list:
    def normalize(s):
        s = s.replace(",", "").replace(" ", "")
        s = re.sub(r'[€$¥]', '', s)
        s = re.sub(r'[亿万]欧元?', '', s)
        return s

    src = normalize(source_text)
    suspicious = []
    for m in _NUM.finditer(copy_text):
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
            bad = check_numbers(copy_text, built["text"])
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

def process_techcrunch(articles: list) -> list:
    """处理 TechCrunch 文章，生成小红书文案"""
    results = []

    for article in articles:
        # 生成一个简短的标题提示
        title_hint = article['title'].replace('|', ' ').replace('\n', ' ')

        copy_text = generate_copy(
            content=f"标题：{article['title']}\n\n摘要：{article['summary']}\n\n原文链接：{article['link']}",
            company_name=", ".join([COMPANIES.get(t, t) for t in article['matched_companies']]),
            ticker=",".join(article['matched_companies'][:3]),
            accepted_et=datetime.now(ET),
            items=["TechCrunch 报道"],
            form="TechCrunch",
            custom_title=title_hint[:100],
            is_routine=False
        )

        results.append({
            "ticker": ",".join(article['matched_companies'][:3]),
            "company": ", ".join([COMPANIES.get(t, t) for t in article['matched_companies']]),
            "form": "TechCrunch",
            "accepted_et": datetime.now(ET),
            "accession": f"TC_{int(time.time())}",
            "items": ["TechCrunch 报道"],
            "url": article['link'],
            "exhibits": [],
            "copy": copy_text,
            "warnings": ["请人工复核数据准确性"] if "【生成失败】" not in copy_text else ["生成失败"],
            "alert": False,
            "source": "TechCrunch",
            "is_routine": False
        })

    return results


# ==================== 输出 ====================

def write_outputs(all_results: list, checked: int):
    OUTPUT_DIR.mkdir(exist_ok=True)
    now_et = datetime.now(ET)

    for r in all_results:
        if r["source"] == "TechCrunch":
            ts = r["accepted_et"].strftime("%Y%m%d-%H%M")
            path = OUTPUT_DIR / f"TC_{ts}_{r['ticker']}.md"
        else:
            ts = r["accepted_et"].strftime("%Y%m%d-%H%M")
            path = OUTPUT_DIR / f"{ts}_{r['ticker']}_{r['accession'][-6:]}.md"

        with path.open("w", encoding="utf-8") as fh:
            fh.write(f"# {r['company']}\n\n")
            fh.write(f"- 提交时间：{r['accepted_et'].strftime('%Y-%m-%d %H:%M')} ET\n")
            fh.write(f"- 类型：{r['form']}　事项：{', '.join(r['items']) or '—'}\n")
            fh.write(f"- 原文：{r['url']}\n")
            if r["exhibits"]:
                for ex in r["exhibits"]:
                    fh.write(f"- 附件：{ex}\n")
            if r["is_routine"]:
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
        fh.write(f"监控公司：{checked} 家　产出：{len(all_results)} 条\n\n")
        fh.write(f"- SEC 申报：{len([r for r in all_results if r['source'] == 'SEC'])} 条\n")
        fh.write(f"- TechCrunch：{len([r for r in all_results if r['source'] == 'TechCrunch'])} 条\n")
        routine_count = len([r for r in all_results if r.get('is_routine', False)])
        if routine_count:
            fh.write(f"- 月度常规披露：{routine_count} 条\n\n")

        alerts = [r for r in all_results if r.get("alert", False)]
        if alerts:
            fh.write("## 🚨 高优先级\n\n")
            for r in alerts:
                fh.write(f"- {r['company']}　{', '.join(r['items'])}　{r['url']}\n")
            fh.write("\n")

        fh.write("---\n\n")
        for i, r in enumerate(all_results, 1):
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
                print(f"    📄 {article['title'][:60]}... -> {', '.join(article['matched_companies'])}")
            all_results.extend(process_techcrunch(tc_articles))
        else:
            print("  ℹ️ 暂无监控公司相关新闻")
    except Exception as e:
        print(f"  ❌ TechCrunch 抓取失败：{e}")

    # ===== 输出 =====
    print("-" * 60)
    if all_results:
        path = write_outputs(all_results, len(COMPANIES))
        sec_count = len([r for r in all_results if r['source'] == 'SEC'])
        tc_count = len([r for r in all_results if r['source'] == 'TechCrunch'])
        routine_count = len([r for r in all_results if r.get('is_routine', False)])
        warned = [r for r in all_results if r["warnings"]]

        print(f"✅ 完成：产出 {len(all_results)} 条文案")
        print(f"   - SEC：{sec_count} 条")
        print(f"   - TechCrunch：{tc_count} 条")
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
