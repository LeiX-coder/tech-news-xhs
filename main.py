#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
海外科技新闻监控 + 小红书文案生成（修订版）

相对上一版的主要改动：
1. 彻底不用 edgartools 做核心路由。Item 代码直接取自 SEC submissions API
   的 items 字段（格式由 SEC 保证），消除 getattr 静默降级。
2. Item 过滤改为白名单；5.02 加关键词二次筛；4.02 单独告警。
3. 修正 .txt 兜底 URL；附件定位改为解析申报索引页的 Type 列。
4. 统一用 SEC 要求的 User-Agent；全局限速 + 429/403 退避重试。
5. 删掉关键词 grep 提数字的做法，改为原文头部 + 指引段落。
6. 全流程美东时间；用 acceptanceDateTime（精确到秒，能区分盘中/盘后）。
7. 加去重（已处理的 accession 落盘）。
8. 异常处理下沉到单份文件粒度。
9. Prompt 改为 星火速报 的既有文案规范。
10. 生成后做数字校验，输出里出现原文没有的数字会告警。
11. 支持 6-K —— NOK / ASML / TSM 是外国发行人，根本不发 8-K。

依赖: pip install requests beautifulsoup4 lxml openai
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

# ==================== 配置区域 ====================

# SEC 强制要求：必须是真实的「名字 邮箱」，伪装成浏览器 UA 会被限流/403
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

# 只处理这些 Item。白名单比黑名单可控：黑名单会被 9.01 / 7.01 这类
# 「几乎每份都带」的附属条目击穿，等于没过滤。
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

# 5.02 里绝大多数是董事会换届等常规事项，靠关键词捞出真正的高管变动
EXEC_CHANGE_KEYWORDS = [
    "resign", "resignation", "step down", "stepping down", "depart",
    "terminate", "termination", "effective immediately", "transition",
    "chief executive", "chief financial", "chief operating",
    "interim", "successor", "retire", "retirement",
]

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
MIN_INTERVAL = 0.12  # SEC 上限 10 req/s，留余量

HEADERS = {
    "User-Agent": SEC_IDENTITY,
    "Accept-Encoding": "gzip, deflate",
}


def sec_get(url: str, max_retries: int = 3, timeout: int = 45):
    """所有对 SEC 的请求都走这里：统一 UA、全局限速、429/403 退避。"""
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
    """ticker -> CIK。SEC 官方映射表。"""
    if _ticker_cache:
        return _ticker_cache
    data = sec_get(f"{SEC}/files/company_tickers.json").json()
    for row in data.values():
        _ticker_cache[row["ticker"].upper()] = int(row["cik_str"])
    return _ticker_cache


def clean_html(html: str) -> str:
    """从 inline XBRL / 普通 HTML 里抽干净正文。

    必须先剔除 ix:header、ix:hidden 和 display:none 的节点，
    否则 get_text() 会混进大量 XBRL 上下文噪声。
    """
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
    """acceptanceDateTime 形如 '2026-07-02T16:05:43.000Z'。

    注意：这个 Z 后缀是误导性的，SEC 存的实际是美东时间，
    不要再当 UTC 做时区转换，否则会差 4~5 小时。
    """
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


# ==================== EDGAR 抓取 ====================

def list_filings(cik: int, forms=("8-K", "6-K"), lookback_days=3):
    """从 submissions API 拉申报列表。

    items 字段由 SEC 直接给出（形如 '2.02,9.01'），不需要解析任何 HTML，
    也就不存在库版本差异导致的静默失败。
    """
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
            # 正确的完整提交文本路径：目录 + 带横杠的 accession
            "full_txt_url": f"{base}/{acc_dash}.txt",
            "primary_url": f"{base}/{rec['primaryDocument'][i]}",
        })

    return out


def list_documents(base: str, accession: str):
    """解析申报索引页，拿到每个文件的 Description / Type / URL。"""
    html = sec_get(f"{base}/{accession}-index.htm").text
    soup = BeautifulSoup(html, "lxml")

    docs = []
    for row in soup.select("table.tableFile tr"):
        cells = [c.get_text(" ", strip=True) for c in row.find_all("td")]
        link = row.find("a")
        if len(cells) < 4 or not link:
            continue
        href = link.get("href", "").split("#")[0]
        # iXBRL 链接被包了一层 viewer，要剥掉才是真实文件地址
        if href.startswith("/ix?doc="):
            href = href[len("/ix?doc="):]
        docs.append({
            "desc": cells[1],
            "doc": link.get_text(strip=True),
            "type": cells[3],
            "url": SEC + href if href.startswith("/") else href,
        })
    return docs


# 无分隔符文件名（ex991.htm）拆分时的候选主编号，长的优先匹配。
# 刻意不含 101/104——那是 XBRL 附件，只会以 'EX-101.INS' 这种
# 带分隔符的形式出现在 Type 列，不会写成裸文件名。
_MAJOR_CANDIDATES = ["99", "32", "31", "24", "23", "21", "10",
                     "1", "2", "3", "4", "5", "7", "8"]


def _exhibit_number(s: str):
    """归一化附件编号。

    'EX-99.1' / 'EX-99.01' / 'ex99-1.htm' / 'ex991.htm' 一律 -> (99, 1)
    'EX-101.INS' -> (101, 0)，不会被误认成 EX-10.1
    """
    s = s.lower()

    # XBRL 附件（EX-101.INS / EX-101.SCH ...）单独识别，
    # 否则会被拆成 (10, 1) 而误认成 EX-10.1
    if re.search(r"ex[\-_]?(\d{3})\.(ins|sch|cal|def|lab|pre)", s):
        return (int(re.search(r"ex[\-_]?(\d{3})", s).group(1)), 0)

    # 先去掉文件扩展名，否则 'ex991.htm' 的那个点会被当成编号分隔符
    s = re.sub(r"\.(htm|html|txt|xml|xsd|pdf|jpe?g|png|gif)$", "", s)

    # 分隔符只有在后面紧跟数字时才算分隔符
    m = re.search(r"ex[\-_]?(\d+)(?:[\.\-_](\d+))?", s)
    if not m:
        return None
    digits, minor = m.group(1), m.group(2)

    if minor is not None:
        return (int(digits), int(minor))

    # 无分隔符：ex991 / ex9901 / dex101 这类，按候选主编号拆。
    # 只在 3 位及以上时拆——'ex21' 到底是 EX-21 还是 EX-2.1 无法判断，
    # 保持原样即可，因为 Type 列（'EX-2.1'）才是权威来源，
    # 文件名只是兜底。
    if len(digits) >= 3:
        for major in _MAJOR_CANDIDATES:
            if digits.startswith(major) and len(digits) > len(major):
                return (int(major), int(digits[len(major):]))
    return (int(digits), 0)


def find_exhibit(docs, major: int, minor: int):
    """按归一化后的编号找附件，容忍 EX-99.1 / EX-99.01 / ex991.htm 各种写法。"""
    for d in docs:
        for field in (d["type"], d["doc"]):
            num = _exhibit_number(field)
            if num and num[0] == major and (num[1] == minor or minor == 0):
                return d
    return None


def extract_press_release(text: str, head_chars=4500, guidance_window=2500) -> str:
    """财报新闻稿处理。

    刻意不做关键词 grep 抽数字——那会把标签和数值拆散、打乱顺序，
    模型只能猜哪个数字对应哪个指标。新闻稿开头本来就是结构化要点
    （营收、分部、EPS），直接给原文最安全。
    唯一的额外处理是：确保末尾的「指引」段落不被截断丢掉。
    """
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
    """正文 + 按需附件。返回 dict，失败抛异常（不静默降级）。"""
    items = filing["items"]
    parts = []

    body = clean_html(sec_get(filing["primary_url"]).text)
    if len(body) < 100:
        raise RuntimeError(f"正文提取过短（{len(body)} 字符），疑似解析失败")
    parts.append(f"【8-K 正文】\n{body[:6000]}")

    exhibits_used = []
    docs = None

    # Item 2.02：正文只有一句「详见附件」，数字全在新闻稿里，必须跟进
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
            # 财报却拿不到新闻稿，宁可报错也不要生成一条没数字的文案
            raise RuntimeError("Item 2.02 但未找到 EX-99.x 新闻稿")

    # 注意：这里刻意【不】抓 EX-10.x。
    # 信贷协议/并购协议原件动辄几百 KB 到几 MB，开头 2000 字符全是
    # 封面和目录，对写稿毫无用处。8-K 正文的法定作用就是概括这些
    # 重大条款，正文已经够了。要抠具体条款时人工去看原件。

    return {
        "text": "\n\n".join(parts),
        "exhibits": exhibits_used,
        "documents": docs,
    }


def should_process(filing: dict) -> tuple:
    """返回 (是否处理, 原因)。"""
    items = filing["items"]

    if filing["form"] == "6-K":
        # 6-K 没有 Item 代码，外国发行人用它发财报和重大公告，数量不多，全收
        return True, "6-K"

    if not items:
        # 8-K 一定有 Item。空说明 API 数据异常，保留并告警
        return True, "⚠️ items 为空（数据异常，保留复核）"

    hit = set(items) & set(WANTED_ITEMS)
    if not hit:
        return False, f"仅含附属条目: {','.join(items)}"

    # 5.02 单独出现时做关键词二次筛，滤掉董事会换届之类的常规事项
    if hit == {"5.02"}:
        return "CHECK_5_02", "5.02 待关键词确认"

    return True, ",".join(sorted(hit))


# ==================== 文案生成 ====================

SYSTEM_PROMPT = """你是 星火速报 的撰稿人，为小红书写美股与科技公司的新闻快讯。

严格遵守以下格式与文风规范：

1. 第一行只写日期，格式为「2026年7月2日」，单独成行。
2. 全文只使用美东时间作为时间参照，绝对不出现北京时间。
3. 第一句话中自然带出消息来源（如「据公司向美国证券交易委员会提交的文件」
   或「据彭博社」），不要用「根据8-K文件」这种生硬表达。
4. 语气克制、平实，不使用夸张词、不使用感叹号堆砌、不做情绪煽动。
   不要写「炸了」「震惊」「速看」这类标题党用语。
5. 只陈述材料中已确认的事实。任何没有出现在材料里的数字、时间、
   人名、因果推断，一律不得写入。宁可少写，不可编造。
6. 涉及财务数据时，必须与材料中的数字完全一致，包括单位和口径
   （如「营收 36.2 亿美元」不可写成「36 亿」）。
7. 结尾另起一行注明来源，格式为「来源：美国证券交易委员会公开文件」
   或具体的新闻稿名称。
8. 全文 300-500 字，不使用 emoji，不使用 markdown 标题符号。
9. 如果材料信息不足以支撑一条完整快讯，直接输出「材料不足」四个字，
   不要勉强凑字数。"""


def generate_copy(content: str, company_name: str, accepted_et: datetime,
                  items: list, form: str) -> str:
    if not DEEPSEEK_API_KEY:
        return "【错误】未设置 DEEPSEEK_API_KEY 环境变量"

    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)

    item_desc = "、".join(
        f"{k} {WANTED_ITEMS.get(k, '其他')}" for k in items
    ) if items else form

    user_prompt = f"""公司：{company_name}
提交时间（美东）：{accepted_et.strftime('%Y-%m-%d %H:%M')} ET
文件类型：{form}
涉及事项：{item_desc}

原始材料：
{content}

请按规范输出完整文案。"""

    try:
        resp = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,           # 事实类写作，压低随机性
            max_tokens=1200,
            timeout=90,
            # V4 默认开启思考模式，批量跑会明显拖慢并增加成本。
            # 注意：思考模式下 temperature 不生效，所以这里必须关掉。
            extra_body={"thinking": {"type": "disabled"}},
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        return f"【生成失败】{e}"


_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def check_numbers(copy_text: str, source_text: str) -> list:
    """校验：文案里出现的数字，原文里是否存在。

    不是万无一失（模型可能做单位换算），但能捞出大部分凭空生成的数字。
    """
    def norm(s):
        return s.replace(",", "")

    src = norm(source_text)
    suspicious = []
    for m in _NUM.finditer(copy_text):
        raw = m.group()
        val = norm(raw)
        # 忽略年份和一位数
        if len(val.replace(".", "")) <= 1:
            continue
        if re.fullmatch(r"(19|20)\d{2}", val):
            continue
        if val not in src:
            suspicious.append(raw)
    return sorted(set(suspicious))


# ==================== 主流程 ====================

def process_company(ticker: str, cik: int, seen: set):
    """返回该公司本次新产出的结果列表。异常粒度下沉到单份文件。"""
    results = []
    filings = list_filings(cik, lookback_days=DAYS_LOOKBACK)

    if not filings:
        print(f"  ℹ️  最近 {DAYS_LOOKBACK} 天无新申报")
        return results

    for f in filings:
        acc = f["accession"]
        if acc in seen:
            print(f"  ⏭️  已处理过: {acc}")
            continue

        decision, reason = should_process(f)
        if decision is False:
            print(f"  ⏭️  跳过 {acc} - {reason}")
            seen.add(acc)
            continue

        try:
            built = build_content(f)
        except Exception as e:
            print(f"  ❌ {acc} 内容提取失败: {e}")
            continue    # 不加入 seen，下次重试

        # 5.02 的关键词二次确认
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
            built["text"], COMPANIES.get(ticker, ticker),
            f["accepted_et"], f["items"], f["form"],
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
        })
        seen.add(acc)
        print(f"  ✅ {acc} 完成（材料 {len(built['text'])} 字符）")

    return results


def write_outputs(all_results: list, checked: int):
    OUTPUT_DIR.mkdir(exist_ok=True)
    now_et = datetime.now(ET)

    for r in all_results:
        ts = r["accepted_et"].strftime("%Y%m%d-%H%M")
        path = OUTPUT_DIR / f"{ts}_{r['ticker']}_{r['accession'][-6:]}.md"
        with path.open("w", encoding="utf-8") as fh:
            fh.write(f"# {r['company']}\n\n")
            fh.write(f"- 提交时间：{r['accepted_et'].strftime('%Y-%m-%d %H:%M')} ET\n")
            fh.write(f"- 类型：{r['form']}　事项：{', '.join(r['items']) or '—'}\n")
            fh.write(f"- 原文：{r['url']}\n")
            for ex in r["exhibits"]:
                fh.write(f"- 附件：{ex}\n")
            if r["warnings"]:
                fh.write(f"- ⚠️ 告警：{'；'.join(r['warnings'])}\n")
            fh.write("\n---\n\n")
            fh.write(r["copy"])

    summary = OUTPUT_DIR / f"summary_{now_et.strftime('%Y%m%d_%H%M')}.md"
    with summary.open("w", encoding="utf-8") as fh:
        fh.write("# SEC 申报监控汇总\n\n")
        fh.write(f"生成时间：{now_et.strftime('%Y-%m-%d %H:%M')} ET\n\n")
        fh.write(f"监控公司：{checked} 家　产出文案：{len(all_results)} 条\n\n")

        alerts = [r for r in all_results if r["alert"]]
        if alerts:
            fh.write("## 🚨 高优先级\n\n")
            for r in alerts:
                fh.write(f"- {r['company']}　{', '.join(r['items'])}　{r['url']}\n")
            fh.write("\n")

        fh.write("---\n\n")
        for i, r in enumerate(all_results, 1):
            fh.write(f"## {i}. {r['company']}"
                     f"（{r['accepted_et'].strftime('%m-%d %H:%M')} ET）\n\n")
            fh.write(f"事项：{', '.join(r['items']) or r['form']}\n\n")
            fh.write(f"原文：{r['url']}\n\n")
            if r["warnings"]:
                fh.write(f"⚠️ {'；'.join(r['warnings'])}\n\n")
            fh.write(r["copy"] + "\n\n---\n\n")

    return summary


def main():
    now_et = datetime.now(ET)
    print(f"🚀 开始运行 - {now_et.strftime('%Y-%m-%d %H:%M:%S')} ET")
    print(f"📋 监控公司：{len(COMPANIES)} 家　回溯：{DAYS_LOOKBACK} 天")
    print("-" * 60)

    try:
        tmap = load_ticker_map()
    except Exception as e:
        print(f"❌ 无法加载 ticker→CIK 映射表：{e}")
        return

    seen = load_seen()
    all_results = []
    failed = []

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
            save_seen(seen)   # 每家跑完就落盘，中途崩溃也不会重复处理

    print("-" * 60)
    if all_results:
        path = write_outputs(all_results, len(COMPANIES))
        warned = [r for r in all_results if r["warnings"]]
        print(f"✅ 完成：产出 {len(all_results)} 条文案")
        if warned:
            print(f"⚠️  {len(warned)} 条带告警，发布前必须人工复核：")
            for r in warned:
                print(f"     {r['ticker']}: {'；'.join(r['warnings'])}")
        if failed:
            print(f"❌ {len(failed)} 家抓取失败：{', '.join(failed)}")
        print(f"📁 汇总：{path.absolute()}")
    else:
        print("📭 本次没有新的可用申报")
        if failed:
            print(f"❌ 抓取失败：{', '.join(failed)}")


if __name__ == "__main__":
    main()
