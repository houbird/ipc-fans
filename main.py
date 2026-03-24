import feedparser
import google.generativeai as genai
import os
from datetime import datetime

# ==========================================
# 1. 設定區 (請填入您的 Gemini API Key)
# ==========================================
GEMINI_API_KEY = "YOUR_GEMINI_API_KEY"  # 請至 https://aistudio.google.com/ 申請
COMPETITORS = ['Advantech', 'Axiomtek', 'Adlink']
TARGET_KEYWORD = "Edge AI OR Industrial PC OR Embedded" # 縮小搜尋範圍，精準打擊

# 初始化 Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash') # 使用 flash 版本，速度快且免費額度高

def fetch_competitor_news():
    """從 Google News 抓取競品相關新聞"""
    query = f"({' OR '.join(COMPETITORS)}) AND ({TARGET_KEYWORD})"
    rss_url = f"https://news.google.com/rss/search?q={query}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    
    feed = feedparser.parse(rss_url)
    print(f"--- 🔍 偵測到 {len(feed.entries)} 則相關動態 ---\n")
    return feed.entries[:8]  # 先取前 8 則進行分析，避免 Token 浪費

def analyze_with_gemini(news_entries):
    """將新聞標題與連結送交 Gemini 進行戰略摘要"""
    
    # 建立 Prompt (CEO 模式：要求分析威脅與類型)
    prompt = f"""
    你現在是一位專業的工業電腦 (IPC) 產業分析師。
    以下是競爭對手 ({', '.join(COMPETITORS)}) 的最新新聞標題。
    請針對這些資訊進行「每日競品情報摘要」。
    
    格式要求：
    1. 項目分類：區分為 [產品發佈]、[市場合作]、[財報動態] 或 [技術亮點]。
    2. 威脅評估：以 1-5 顆星標示對 Aetina (我司) 的潛在威脅。
    3. 關鍵摘要：用一句話說明該新聞的重點。
    4. 建議行動：我們應該關注什麼？
    
    新聞清單：
    {chr(10).join([f"- 標題: {e.title} (來源: {e.source.text})" for e in news_entries])}
    """
    
    try:
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        return f"❌ AI 辨識發生錯誤: {str(e)}"

def generate_report():
    print(f"📅 執行時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    entries = fetch_competitor_news()
    if not entries:
        print("今日無相關重要新聞。")
        return

    print("🤖 Gemini 正在分析中，請稍候...")
    report = analyze_with_gemini(entries)
    
    # 輸出最終 Report
    print("\n" + "="*50)
    print("        🏆 Aetina 競品戰略情報每日報告 🏆")
    print("="*50)
    print(report)
    print("="*50)
    print("\n報告生成完畢。連結參考：")
    for e in entries:
        print(f"🔗 {e.title} -> {e.link}")

if __name__ == "__main__":
    generate_report()
