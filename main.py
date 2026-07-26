# -*- coding: utf-8 -*-
"""
海外科技新闻监控 + 小红书文案生成
功能：
1. 拉取指定公司最新的 8-K 文件
2. 用 DeepSeek 生成小红书风格中文文案
3. 把结果保存到 output/ 目录
"""

import os
from datetime import datetime, timedelta
from pathlib import Path

from edgar import set_identity, Company
from openai import OpenAI

# ==================== 配置区域 ====================

# 1. 你的身份（SEC 要求，随便写真实一点的邮箱就行）
SEC_IDENTITY = "xiaolei xiaolei12555@126.com"

# 2. 要监控的公司（ticker + 中文名）
COMPANIES = {
    # ===== 你指定的公司 =====
    "INTC": "英特尔 Intel",
    "NOK": "诺基亚 Nokia",
    "AAOI": "Applied Optoelectronics",
    "LITE": "Lumentum Holdings",
    "MU": "美光 Micron",
    
    # ===== 科技巨头 =====
    "AAPL": "苹果 Apple",
    "MSFT": "微软 Microsoft",
    "NVDA": "英伟达 Nvidia",
    "GOOGL": "谷歌 Alphabet",
    "AMZN": "亚马逊 Amazon",
    "META": "Meta",
    "AVGO": "博通 Broadcom",
    "TSM": "台积电 TSMC",
    
    # ===== 半导体 =====
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
    "WDC": "西部数据 Western Digital",  # 包含闪迪
    
    # ===== 软件/云 =====
    "SNPS": "新思科技 Synopsys",
    "CDNS": "楷登电子 Cadence",
    "CSCO": "思科 Cisco",
    "PANW": "Palo Alto Networks",
    "CRWD": "CrowdStrike",
    "ZS": "Zscaler",
    "FTNT": "Fortinet",
    "PLTR": "Palantir",
    "ANET": "Arista Networks",
    
    # ===== 其他科技 =====
    "STX": "希捷 Seagate",
}

# 3. 只看最近几天的文件（防止一次拉太多）
DAYS_LOOKBACK = 30

# 4. DeepSeek 配置（从环境变量读取，不要写死在代码里）
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-chat"  # 正确的模型名

# ==================== 以下代码一般不用改 ====================

def init_edgar():
    """初始化 SEC 身份"""
    set_identity(SEC_IDENTITY)

def get_recent_8k(ticker: str, days: int = 3):
    """获取某家公司最近几天的 8-K 文件"""
    try:
        company = Company(ticker)
        # 获取最近的 8-K
        filings = company.get_filings(form="8-K").head(10)  # 先取最近10个
        
        results = []
        cutoff = datetime.now() - timedelta(days=days)
        
        for filing in filings:
            # filing.filing_date 是 date 对象
            if filing.filing_date >= cutoff.date():
                # 提取文件内容
                text = "无法提取正文，请手动查看原文。"
                url = "N/A"
                
                try:
                    # 方法1：尝试获取 8-K 对象
                    eight_k = filing.obj()
                    
                    # 提取 items 内容
                    if hasattr(eight_k, 'items') and eight_k.items:
                        text_parts = []
                        for item in eight_k.items:
                            # 提取 item 的标题和描述
                            item_desc = getattr(item, 'description', '')
                            item_text = getattr(item, 'text', '')
                            
                            if item_desc or item_text:
                                part = f"Item: {item_desc}\n{item_text[:500]}"
                                text_parts.append(part)
                        
                        if text_parts:
                            text = "\n\n".join(text_parts)
                        else:
                            # 如果没有 items，尝试获取整个文件内容
                            text = str(eight_k)
                    else:
                        text = str(eight_k)
                    
                    # 限制长度避免 token 爆炸
                    if len(text) > 8000:
                        text = text[:8000] + "\n\n...（内容过长已截断）"
                    
                    # 获取正确的 URL
                    if hasattr(filing, 'primary_document_url'):
                        url = filing.primary_document_url
                    elif hasattr(filing, 'homepage_url'):
                        url = filing.homepage_url
                    else:
                        # 构造备用 URL
                        cik = getattr(company, 'cik', '')
                        acc_no = filing.accession_number.replace('-', '')
                        url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_no}/"
                        
                except Exception as e:
                    print(f"  ⚠️ 解析 {ticker} 的 8-K 内容失败: {e}")
                    # 降级方案：尝试获取原始文本
                    try:
                        if hasattr(filing, 'text'):
                            text = filing.text[:8000]
                    except:
                        pass
                
                results.append({
                    "ticker": ticker,
                    "company": COMPANIES.get(ticker, ticker),
                    "date": str(filing.filing_date),
                    "accession": filing.accession_number,
                    "url": url,
                    "text": text
                })
        return results
    except Exception as e:
        print(f"[错误] 获取 {ticker} 失败: {e}")
        return []

def generate_xiaohongshu(content: str, company_name: str, date: str) -> str:
    """调用 DeepSeek 生成小红书文案"""
    if not DEEPSEEK_API_KEY:
        return "【错误】未设置 DEEPSEEK_API_KEY 环境变量"
    
    client = OpenAI(
        api_key=DEEPSEEK_API_KEY,
        base_url=DEEPSEEK_BASE_URL
    )
    
    system_prompt = """你是一个擅长写小红书科技类笔记的博主。
请把下面的英文/正式文件内容，改写成适合小红书发布的中文文案。

要求：
1. 标题要有吸引力，可以带数字或情绪词（比如"重磅""刚刚""2000亿"）
2. 正文口语化、有网感，适当使用 emoji
3. 结构清晰：开头抛出重点 → 中间讲清楚发生了什么 → 结尾给一点个人看法或互动问题
4. 控制在 300-500 字左右，适合手机阅读
5. 不要出现"根据文件""根据8-K"这类生硬表达，要像真人分享新闻
6. 如果内容涉及金额，尽量用中文习惯表达（比如2000亿美元）"""

    # 确保 content 不为空
    if not content or content == "无法提取正文，请手动查看原文。":
        content = "该公司今日提交了 8-K 文件，详细内容请查看 SEC 官网。"

    user_prompt = f"""公司：{company_name}
日期：{date}

原始内容：
{content[:6000]}

请直接输出完整的小红书文案（包含标题），不要加其他解释。"""

    try:
        response = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.7,
            max_tokens=1024,
            timeout=60
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"【生成失败】{e}"

def main():
    print(f"🚀 开始运行 - {datetime.now()}")
    print(f"📋 监控公司数: {len(COMPANIES)} 家")
    print(f"📋 公司列表: {', '.join(COMPANIES.keys())}")
    print("-" * 50)
    
    init_edgar()
    
    output_dir = Path("output")
    output_dir.mkdir(exist_ok=True)
    
    all_results = []
    total_filings = 0
    failed_companies = []
    
    for idx, ticker in enumerate(COMPANIES, 1):
        print(f"[{idx}/{len(COMPANIES)}] 📡 正在检查 {ticker} ({COMPANIES[ticker]}) ...")
        filings = get_recent_8k(ticker, DAYS_LOOKBACK)
        
        if not filings:
            print(f"  ℹ️  {ticker} 最近 {DAYS_LOOKBACK} 天没有新的 8-K 文件")
            continue
        
        print(f"  ✅ 发现 {len(filings)} 个 8-K 文件")
        total_filings += len(filings)
        
        for f in filings:
            print(f"    📄 处理: {f['date']} - {f['accession'][:10]}...")
            xhs_content = generate_xiaohongshu(
                content=f["text"],
                company_name=f["company"],
                date=f["date"]
            )
            
            # 检查是否生成失败
            if xhs_content.startswith("【生成失败】") or xhs_content.startswith("【错误】"):
                failed_companies.append(f"{ticker} ({f['date']})")
            
            result = {
                "ticker": f["ticker"],
                "company": f["company"],
                "date": f["date"],
                "url": f["url"],
                "xiaohongshu": xhs_content
            }
            all_results.append(result)
            
            # 保存单个文件
            filename = f"{f['date']}_{f['ticker']}.md"
            filepath = output_dir / filename
            with open(filepath, "w", encoding="utf-8") as file:
                file.write(f"# {f['company']} - {f['date']}\n\n")
                file.write(f"📎 原文链接: {f['url']}\n\n")
                file.write("---\n\n")
                file.write(xhs_content)
            print(f"      💾 已保存: {filename}")
    
    # 保存汇总
    if all_results:
        summary_filename = f"summary_{datetime.now().strftime('%Y%m%d_%H%M')}.md"
        summary_path = output_dir / summary_filename
        
        with open(summary_path, "w", encoding="utf-8") as f:
            f.write(f"# 📊 今日 SEC 8-K 监控汇总\n\n")
            f.write(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            f.write(f"**监控公司数**: {len(COMPANIES)}\n")
            f.write(f"**发现文件数**: {total_filings}\n")
            f.write(f"**成功生成文案**: {len(all_results) - len(failed_companies)} 条\n")
            if failed_companies:
                f.write(f"**失败列表**: {', '.join(failed_companies)}\n")
            f.write("\n---\n\n")
            
            for idx, r in enumerate(all_results, 1):
                f.write(f"## {idx}. {r['company']} ({r['date']})\n\n")
                f.write(f"🔗 原文: {r['url']}\n\n")
                f.write(r["xiaohongshu"])
                f.write("\n\n---\n\n")
        
        print("-" * 50)
        print(f"✅ 任务完成！")
        print(f"📊 共发现 {total_filings} 个 8-K 文件")
        print(f"📝 成功生成 {len(all_results) - len(failed_companies)} 条文案")
        if failed_companies:
            print(f"⚠️  {len(failed_companies)} 条生成失败: {', '.join(failed_companies)}")
        print(f"📁 已保存到: {output_dir.absolute()}/")
        print(f"📄 汇总文件: {summary_filename}")
        
        # 打印第一条预览
        if all_results:
            print("\n" + "="*50)
            print("📱 第一条文案预览:")
            print("="*50)
            first = all_results[0]
            print(f"【{first['company']}】")
            print(first['xiaohongshu'][:500] + "...\n")
    else:
        print("-" * 50)
        print("📭 今天没有发现新的相关 8-K 文件")

if __name__ == "__main__":
    main()
