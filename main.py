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

# ==================== 配置区域（你只需要改这里） ====================

# 1. 你的身份（SEC 要求，随便写真实一点的邮箱就行）
SEC_IDENTITY = "xiaolei xiaolei12555@126.com"

# 2. 要监控的公司（ticker + 中文名）
COMPANIES = {
    "AVGO": "博通 Broadcom",
    "NVDA": "英伟达 Nvidia",
    "TSM": "台积电 TSMC",
    "AAPL": "苹果 Apple",
    "MSFT": "微软 Microsoft",
    "GOOGL": "谷歌 Alphabet",
    "AMZN": "亚马逊 Amazon",
    "META": "Meta",
}

# 3. 只看最近几天的文件（防止一次拉太多）
DAYS_LOOKBACK = 3

# 4. DeepSeek 配置（从环境变量读取，不要写死在代码里）
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-v4-flash"   # 便宜又快，也可以改成 deepseek-v4-pro

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
                # 尝试提取文字内容
                try:
                    text = filing.text()
                    # 太长就截断（避免 token 爆炸）
                    if len(text) > 8000:
                        text = text[:8000] + "\n\n...（内容过长已截断）"
                except Exception:
                    text = "无法提取正文，请手动查看原文。"
                
                results.append({
                    "ticker": ticker,
                    "company": COMPANIES.get(ticker, ticker),
                    "date": str(filing.filing_date),
                    "accession": filing.accession_number,
                    "url": filing.homepage_url,
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
1. 标题要有吸引力，可以带数字或情绪词（比如“重磅”“刚刚”“2000亿”）
2. 正文口语化、有网感，适当使用 emoji
3. 结构清晰：开头抛出重点 → 中间讲清楚发生了什么 → 结尾给一点个人看法或互动问题
4. 控制在 300-500 字左右，适合手机阅读
5. 不要出现“根据文件”“根据8-K”这类生硬表达，要像真人分享新闻
6. 如果内容涉及金额，尽量用中文习惯表达（比如2000亿美元）
"""

    user_prompt = f"""公司：{company_name}
日期：{date}

原始内容：
{content}

请直接输出完整的小红书文案（包含标题），不要加其他解释。"""

    try:
        response = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.7,
            max_tokens=1024
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"【生成失败】{e}"

def main():
    print(f"开始运行 - {datetime.now()}")
    init_edgar()
    
    output_dir = Path("output")
    output_dir.mkdir(exist_ok=True)
    
    all_results = []
    
    for ticker in COMPANIES:
        print(f"正在检查 {ticker} ...")
        filings = get_recent_8k(ticker, DAYS_LOOKBACK)
        
        for f in filings:
            print(f"  发现新文件: {f['date']} - {f['accession']}")
            xhs_content = generate_xiaohongshu(
                content=f["text"],
                company_name=f["company"],
                date=f["date"]
            )
            
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
            with open(output_dir / filename, "w", encoding="utf-8") as file:
                file.write(f"# {f['company']} - {f['date']}\n\n")
                file.write(f"原文链接: {f['url']}\n\n")
                file.write("---\n\n")
                file.write(xhs_content)
    
    # 保存汇总
    if all_results:
        summary_path = output_dir / f"summary_{datetime.now().strftime('%Y%m%d_%H%M')}.md"
        with open(summary_path, "w", encoding="utf-8") as f:
            f.write(f"# 今日监控汇总 ({datetime.now().strftime('%Y-%m-%d %H:%M')})\n\n")
            for r in all_results:
                f.write(f"## {r['company']} ({r['date']})\n")
                f.write(f"原文: {r['url']}\n\n")
                f.write(r["xiaohongshu"])
                f.write("\n\n---\n\n")
        print(f"共生成 {len(all_results)} 条内容，已保存到 output/ 目录")
    else:
        print("今天没有发现新的相关 8-K 文件")

if __name__ == "__main__":
    main()
