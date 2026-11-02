#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TWSE 競價拍賣公告爬蟲、公司基本面/財務數據爬蟲與新聞摘要模組
抓取 https://www.twse.com.tw/zh/announcement/auction.html (RWD JSON API)
僅篩選「尚未結束競拍」之標的，並自動抓取：
1. 公司介紹摘要（所屬行業、主要業務範圍、產品營收比重、營收規模、毛利率/營益率/淨利率/EPS 等財務指標）
2. Google News 最新相關財經新聞整理與連結
"""

import datetime
import html
import json
import os
import re
import ssl
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional


TWSE_AUCTION_PAGE_URL = "https://www.twse.com.tw/zh/announcement/auction.html"
TWSE_AUCTION_API_URL = "https://www.twse.com.tw/rwd/zh/announcement/auction"
GOOGLE_NEWS_RSS_URL = "https://news.google.com/rss/search"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)


def _create_ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context()


def _http_get(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/html, text/plain, */*",
            "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_create_ssl_context()) as resp:
            return resp.read()
    except Exception:
        unverified_ctx = ssl._create_unverified_context()
        with urllib.request.urlopen(req, timeout=timeout, context=unverified_ctx) as resp:
            return resp.read()


def parse_twse_date(date_str: str) -> Optional[datetime.date]:
    """解析證交所日期字串 (支援西元年 YYYY/MM/DD 或民國年 YYY/MM/DD)"""
    if not date_str:
        return None
    s = date_str.strip()
    parts = re.split(r"[/\-.]", s)
    if len(parts) != 3:
        return None
    try:
        year = int(parts[0])
        month = int(parts[1])
        day = int(parts[2])
        if year < 1911:
            year += 1911
        return datetime.date(year, month, day)
    except ValueError:
        return None


def parse_float(val: str) -> float:
    if not val:
        return 0.0
    cleaned = re.sub(r"[,%\s]", "", str(val))
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def format_thousand_ntd_to_yi(val_str: str) -> str:
    """將『仟元』單位數字轉為易讀的『億元』或『萬元』字串"""
    num = parse_float(val_str)
    if num <= 0:
        return val_str
    # num 單位為仟元 -> 乘以 1000 為元，除以 1e8 為億元 (即 num / 100,000)
    yi = num / 100000.0
    if yi >= 1.0:
        return f"{yi:.2f} 億元"
    wan = num / 10.0
    return f"{wan:,.0f} 萬元"


def get_underlying_code_and_name(name: str, code: str) -> Dict[str, str]:
    """若為可轉債（如 53814 光譜四、33395 泰谷五），取得其 4 碼母股代號與母公司名稱"""
    name = name.strip()
    code = code.strip()
    base_name = re.sub(r"[一二三四五六七八九十]+$", "", name).strip()
    base_code = code[:4] if len(code) >= 5 and code[:4].isdigit() else code
    return {"base_name": base_name or name, "base_code": base_code or code}


def clean_company_search_name(name: str, code: str) -> List[str]:
    """
    產生精準的財經新聞搜尋關鍵字清單。
    若是可轉債（如「泰谷五 33395」、「光譜四 53814」），也會自動納入母股代號與財經關鍵字，避免抓到一般生活/展覽新聞。
    """
    info = get_underlying_code_and_name(name, code)
    base_name = info["base_name"]
    base_code = info["base_code"]

    queries = [
        f'"{name}"',
        f'"{base_name}" "{base_code}"',
        f'"{base_name}" (競拍 OR 轉換公司債 OR 申購 OR 股價 OR 營收 OR 財報)',
    ]

    seen = set()
    unique_queries = []
    for q in queries:
        if q and q not in seen:
            seen.add(q)
            unique_queries.append(q)
    return unique_queries


def _extract_moneydj_cells(page_prefix: str, base_code: str) -> List[str]:
    """抓取 MoneyDJ 個股基本面子頁面並解析所有表格欄位文字"""
    url = f"https://justdata.moneydj.com/z/zc/{page_prefix}/{page_prefix}_{base_code}.djhtm"
    try:
        raw_bytes = _http_get(url, timeout=12)
        raw_html = raw_bytes.decode("big5", "ignore")
        cells = re.findall(r"<td[^>]*>(.*?)</td>", raw_html, flags=re.S | re.I)
        cleaned = []
        for c in cells:
            t = re.sub(r"<[^>]+>", " ", c)
            t = html.unescape(t).replace("\xa0", " ").strip()
            t = re.sub(r"\s+", " ", t)
            if t and len(t) < 250:
                cleaned.append(t)
        return cleaned
    except Exception as e:
        print(f"[WARN] MoneyDJ {page_prefix}_{base_code} 讀取失敗: {e}")
        return []


def _infer_industry_from_business(business_text: str, market: str) -> str:
    """當興櫃/準上櫃公司尚無掛牌分類時，根據營收比重與業務文字推論所屬行業"""
    text = business_text or ""
    mapping = [
        (["晶圓", "半導體", "二次配", "廠務", "CoWoS", "封測", "IC", "晶片"], "半導體廠務工程 / 設備材料業"),
        (["變壓器", "重電", "配電", "電力", "電工", "馬達"], "電機機械 / 重電設備業"),
        (["發光二極體", "LED", "雷射", "磊晶", "光電", "光學", "面板"], "光電 / 化合物半導體業"),
        (["生技", "新藥", "醫療", "醫材", "製藥", "保健"], "生技醫療業"),
        (["軟體", "雲端", "資訊", "資安", "AI", "系統整合"], "數位雲端 / 資訊服務業"),
        (["伺服器", "散熱", "印刷電路板", "PCB", "連接器", "電源"], "電子零組件 / 電腦週邊業"),
        (["餐飲", "食品", "零售", "百貨", "居家", "貿易"], "民生消費 / 居家貿易業"),
    ]
    for keywords, label in mapping:
        for kw in keywords:
            if kw in text:
                return label
    return f"{market}企業" if market else "一般產業"


def fetch_company_profile_and_financials(
    security_name: str, security_code: str, market: str = ""
) -> Dict[str, Any]:
    """
    自動抓取並整理公司介紹摘要：
    1. 所屬行業 (產業類別)
    2. 主要業務範圍與產品營收比重
    3. 營收規模（最新單月營收、累計營收與年增率、最新單季營收）
    4. 利潤率與獲利能力（營業毛利率、營業利益率、稅前淨利率、ROE、ROA、EPS、每股淨值）
    """
    info = get_underlying_code_and_name(security_name, security_code)
    base_code = info["base_code"]
    base_name = info["base_name"]

    profile: Dict[str, Any] = {
        "base_code": base_code,
        "base_name": base_name,
        "industry": "",
        "business_scope": "",
        "revenue_mix": "",
        "capital_text": "",
        "chairman": "",
        "founded_date": "",
        "website": "",
        # 獲利能力與利潤率
        "profit_period": "",
        "gross_margin": "",
        "operating_margin": "",
        "pretax_margin": "",
        "roe": "",
        "roa": "",
        "bps": "",
        "debt_ratio": "",
        # 營收規模與 EPS
        "latest_month_label": "",
        "latest_month_revenue": "",
        "latest_month_yoy": "",
        "accumulated_revenue": "",
        "accumulated_yoy": "",
        "latest_quarter_label": "",
        "latest_quarter_revenue": "",
        "latest_quarter_net_income": "",
        "latest_quarter_eps": "",
        "recent_4q_eps": "",
        "summary_text": "",
    }

    # Step 1: 抓取 Yahoo 股市 Profile (取得產業類別與主要經營業務)
    for sym in [f"{base_code}.TWO", f"{base_code}.TW", base_code]:
        y_url = f"https://tw.stock.yahoo.com/quote/{sym}/profile"
        try:
            raw_html = _http_get(y_url, timeout=10).decode("utf-8", "ignore")
            blocks = [
                t.strip()
                for t in re.findall(r">([^<>]{1,150})<", raw_html)
                if t.strip()
            ]
            for i, tok in enumerate(blocks):
                if tok == "產業類別" and i + 1 < len(blocks):
                    cand = blocks[i + 1]
                    if "COMPANY_" not in cand and len(cand) < 25:
                        profile["industry"] = cand
                elif tok == "主要經營業務" and i + 1 < len(blocks):
                    cand = blocks[i + 1]
                    if "COMPANY_" not in cand:
                        profile["business_scope"] = re.sub(r"\s+", "、", cand.strip())
            if profile["industry"] or profile["business_scope"]:
                break
        except Exception:
            pass

    # Step 2: 抓取 MoneyDJ zca_{base_code} (基本資料、營收比重、毛利率、營業利益率、淨利率、ROE、每股淨值)
    zca_cells = _extract_moneydj_cells("zca", base_code)
    for i, c in enumerate(zca_cells):
        if c.startswith("獲利能力(") and not profile["profit_period"]:
            m = re.search(r"\((.*?)\)", c)
            if m:
                profile["profit_period"] = m.group(1)
        elif c == "營業毛利率" and i + 1 < len(zca_cells):
            profile["gross_margin"] = zca_cells[i + 1]
        elif c == "營業利益率" and i + 1 < len(zca_cells):
            profile["operating_margin"] = zca_cells[i + 1]
        elif c == "稅前淨利率" and i + 1 < len(zca_cells):
            profile["pretax_margin"] = zca_cells[i + 1]
        elif c == "股東權益報酬率" and i + 1 < len(zca_cells):
            profile["roe"] = zca_cells[i + 1]
        elif c == "資產報酬率" and i + 1 < len(zca_cells):
            profile["roa"] = zca_cells[i + 1]
        elif c == "每股淨值(元)" and i + 1 < len(zca_cells):
            profile["bps"] = zca_cells[i + 1]
        elif c == "負債比例" and i + 1 < len(zca_cells):
            profile["debt_ratio"] = zca_cells[i + 1]
        elif c == "營收比重" and i + 1 < len(zca_cells):
            profile["revenue_mix"] = zca_cells[i + 1]
        elif c in ("股本(億, 台幣)", "股本(百萬, 台幣)") and i + 1 < len(zca_cells):
            val = zca_cells[i + 1]
            if val and val != "0.00" and val != "N/A":
                unit = "億元" if "億" in c else "百萬元"
                profile["capital_text"] = f"{val} {unit}"
        elif c == "董事長" and i + 1 < len(zca_cells):
            profile["chairman"] = zca_cells[i + 1]
        elif c == "成立時間" and i + 1 < len(zca_cells):
            profile["founded_date"] = zca_cells[i + 1]
        elif c == "網址" and i + 1 < len(zca_cells):
            if zca_cells[i + 1].startswith("http"):
                profile["website"] = zca_cells[i + 1]

    # Step 3: 抓取 MoneyDJ zch_{base_code} (最新月營收與累計營收規模)
    zch_cells = _extract_moneydj_cells("zch", base_code)
    for i, c in enumerate(zch_cells):
        # 尋找第一個格式如 "115/09" 或 "115/10" 的月份列
        if re.match(r"^\d{3}/\d{2}$", c) and i + 6 < len(zch_cells):
            profile["latest_month_label"] = c
            raw_month_rev = zch_cells[i + 1]      # 單月營收 (仟元)
            raw_yoy = zch_cells[i + 4]            # 單月年增率
            raw_acc_rev = zch_cells[i + 5]        # 累計營收 (仟元)
            raw_acc_yoy = zch_cells[i + 6]        # 累計年增率
            profile["latest_month_revenue"] = format_thousand_ntd_to_yi(raw_month_rev)
            profile["latest_month_yoy"] = raw_yoy
            profile["accumulated_revenue"] = format_thousand_ntd_to_yi(raw_acc_rev)
            profile["accumulated_yoy"] = raw_acc_yoy
            break

    # Step 4: 抓取 MoneyDJ zcd_{base_code} (最新季報營業收入、稅後淨利與 EPS)
    zcd_cells = _extract_moneydj_cells("zcd", base_code)
    eps_list: List[float] = []
    for i, c in enumerate(zcd_cells):
        if re.match(r"^\d{3}\.[1-4]Q$", c) and i + 7 < len(zcd_cells):
            if not profile["latest_quarter_label"]:
                profile["latest_quarter_label"] = c
                q_rev_m = parse_float(zcd_cells[i + 2])  # 百萬元
                q_net_m = parse_float(zcd_cells[i + 4])  # 百萬元
                q_eps = zcd_cells[i + 7]
                profile["latest_quarter_revenue"] = (
                    f"{q_rev_m / 100.0:.2f} 億元" if q_rev_m >= 100 else f"{q_rev_m:,.0f} 百萬元"
                )
                profile["latest_quarter_net_income"] = (
                    f"{q_net_m / 100.0:.2f} 億元"
                    if abs(q_net_m) >= 100
                    else f"{q_net_m:,.0f} 百萬元"
                )
                profile["latest_quarter_eps"] = f"{q_eps} 元"
            if len(eps_list) < 4:
                try:
                    eps_list.append(float(zcd_cells[i + 7]))
                except ValueError:
                    pass

    if eps_list:
        profile["recent_4q_eps"] = f"{sum(eps_list):.2f} 元 (近{len(eps_list)}季累計)"

    # 若產業類別為空，自動由產品與營收比重推論
    combined_biz = " ".join([profile["business_scope"], profile["revenue_mix"]]).strip()
    if not profile["industry"]:
        profile["industry"] = _infer_industry_from_business(combined_biz, market)

    if not profile["business_scope"] and profile["revenue_mix"]:
        profile["business_scope"] = profile["revenue_mix"]

    # 組裝完整易讀的「公司介紹與財務基本面摘要」
    parts = []
    ind_str = profile["industry"] or "上市櫃/興櫃企業"
    biz_parts = []
    if profile["business_scope"]:
        biz_parts.append(f"主要經營業務為「{profile['business_scope']}」")
    if profile["revenue_mix"] and profile["revenue_mix"] != profile["business_scope"]:
        biz_parts.append(f"產品營收結構為：{profile['revenue_mix']}")
    biz_sentence = "，".join(biz_parts) if biz_parts else "從事相關專業製造與服務業務"
    parts.append(f"【產業與業務】{base_name}（{base_code}）隸屬於「{ind_str}」，{biz_sentence}。")

    # 營收規模句
    rev_items = []
    if profile["latest_month_revenue"]:
        rev_items.append(
            f"最新 ({profile['latest_month_label']}) 單月營收約 {profile['latest_month_revenue']}（年增率 {profile['latest_month_yoy']}）"
        )
    if profile["accumulated_revenue"]:
        rev_items.append(
            f"當年度累計營收達 {profile['accumulated_revenue']}（累計年增率 {profile['accumulated_yoy']}）"
        )
    if profile["latest_quarter_revenue"]:
        rev_items.append(
            f"最新季報 ({profile['latest_quarter_label']}) 單季營收 {profile['latest_quarter_revenue']}、稅後淨利 {profile['latest_quarter_net_income']}"
        )
    if rev_items:
        parts.append("【營收規模】" + "；".join(rev_items) + "。")

    # 利潤率與獲利表現句
    prof_items = []
    if profile["gross_margin"] and profile["gross_margin"] != "N/A":
        prof_items.append(f"營業毛利率 {profile['gross_margin']}")
    if profile["operating_margin"] and profile["operating_margin"] != "N/A":
        prof_items.append(f"營業利益率 {profile['operating_margin']}")
    if profile["pretax_margin"] and profile["pretax_margin"] != "N/A":
        prof_items.append(f"稅前淨利率 {profile['pretax_margin']}")
    if profile["roe"] and profile["roe"] != "N/A":
        prof_items.append(f"ROE {profile['roe']}")
    if profile["latest_quarter_eps"]:
        prof_items.append(f"最新單季 EPS {profile['latest_quarter_eps']}")
    if profile["recent_4q_eps"]:
        prof_items.append(f"近四季累計 EPS {profile['recent_4q_eps']}")
    if profile["bps"] and profile["bps"] != "N/A":
        prof_items.append(f"每股淨值 {profile['bps']} 元")

    if prof_items:
        period_tag = f" ({profile['profit_period']})" if profile["profit_period"] else ""
        parts.append(f"【利潤率與獲利表現{period_tag}】" + "、".join(prof_items) + "。")

    profile["summary_text"] = " ".join(parts)
    return profile


def fetch_twse_auctions(
    reference_date: Optional[datetime.date] = None,
    only_stocks: bool = True,
) -> List[Dict[str, Any]]:
    """
    從 TWSE 抓取競價拍賣公告，並只保留「尚未結束競拍」的股票標的（預設排除可轉債 CB）。
    尚未結束競拍定義：
    - 取消競價拍賣欄位為空
    - 且 (投標結束日 >= 今日 OR 開標日期 >= 今日)
    """
    if reference_date is None:
        tw_tz = datetime.timezone(datetime.timedelta(hours=8))
        reference_date = datetime.datetime.now(tw_tz).date()

    years_to_query = [reference_date.year]
    if reference_date.month >= 11:
        years_to_query.append(reference_date.year + 1)

    raw_rows = []
    fields = []

    for yr in years_to_query:
        query_date = f"{yr}0101"
        url = f"{TWSE_AUCTION_API_URL}?date={query_date}&response=json"
        try:
            raw_bytes = _http_get(url)
            payload = json.loads(raw_bytes.decode("utf-8"))
            if payload.get("stat") == "OK" and payload.get("data"):
                if not fields:
                    fields = payload.get("fields", [])
                raw_rows.extend(payload.get("data", []))
        except Exception as e:
            print(f"[WARN] 無法取得 {yr} 年 TWSE 競拍資料: {e}")

    if not raw_rows:
        url = f"{TWSE_AUCTION_API_URL}?response=json"
        raw_bytes = _http_get(url)
        payload = json.loads(raw_bytes.decode("utf-8"))
        fields = payload.get("fields", [])
        raw_rows = payload.get("data", [])

    active_auctions: List[Dict[str, Any]] = []
    seen_keys = set()

    for row in raw_rows:
        if len(row) < 17:
            continue

        open_bid_date_str = str(row[1]).strip()
        sec_name = str(row[2]).strip()
        sec_code = str(row[3]).strip()
        market = str(row[4]).strip()
        issue_type = str(row[5]).strip()
        auction_method = str(row[6]).strip()
        bid_start_str = str(row[7]).strip()
        bid_end_str = str(row[8]).strip()
        auction_qty_str = str(row[9]).strip()
        min_price_str = str(row[10]).strip()
        min_bid_qty_str = str(row[11]).strip()
        max_bid_qty_str = str(row[12]).strip()
        margin_pct_str = str(row[13]).strip()
        fee_str = str(row[14]).strip()
        listing_date_str = str(row[15]).strip()
        underwriter = str(row[16]).strip()

        total_award_amt = str(row[17]).strip() if len(row) > 17 else "0"
        award_fee_rate = str(row[18]).strip() if len(row) > 18 else "0"
        cancel_status = str(row[25]).strip() if len(row) > 25 else ""

        if cancel_status:
            continue

        open_bid_date = parse_twse_date(open_bid_date_str)
        bid_start_date = parse_twse_date(bid_start_str)
        bid_end_date = parse_twse_date(bid_end_str)

        is_bidding_active = bid_end_date is not None and bid_end_date >= reference_date
        is_waiting_open = open_bid_date is not None and open_bid_date >= reference_date

        if not (is_bidding_active or is_waiting_open):
            continue

        unique_key = f"{sec_code}_{bid_start_str}_{open_bid_date_str}"
        if unique_key in seen_keys:
            continue
        seen_keys.add(unique_key)

        if bid_start_date and reference_date < bid_start_date:
            status_code = "upcoming"
            status_label = "即將開始投標"
            days_left = (bid_start_date - reference_date).days
            countdown_text = f"距離開始投標還有 {days_left} 天"
        elif bid_end_date and reference_date <= bid_end_date:
            status_code = "bidding"
            status_label = "投標進行中"
            days_left = (bid_end_date - reference_date).days
            countdown_text = "今日截止投標！" if days_left == 0 else f"距離截標還有 {days_left} 天"
        else:
            status_code = "waiting_open"
            status_label = "已截標・待開標"
            days_left = (open_bid_date - reference_date).days if open_bid_date else 0
            countdown_text = "今日開標！" if days_left == 0 else f"距離開標還有 {days_left} 天"

        min_price = parse_float(min_price_str)
        min_qty = max(1.0, parse_float(min_bid_qty_str))
        margin_pct = parse_float(margin_pct_str)
        processing_fee = parse_float(fee_str)
        estimated_min_bid_value = int(round(min_price * 1000 * min_qty))
        estimated_min_margin = int(
            round(estimated_min_bid_value * (margin_pct / 100.0) + processing_fee)
        )

        is_cb = "債" in issue_type
        if only_stocks and is_cb:
            continue
        security_category = "可轉債 (CB)" if is_cb else "股票 (IPO/現增)"
        base_code = sec_code[:4] if is_cb and len(sec_code) >= 5 else sec_code

        active_auctions.append(
            {
                "security_name": sec_name,
                "security_code": sec_code,
                "base_code": base_code,
                "market": market,
                "issue_type": issue_type,
                "security_category": security_category,
                "is_cb": is_cb,
                "auction_method": auction_method,
                "bid_start_date": bid_start_str,
                "bid_end_date": bid_end_str,
                "open_bid_date": open_bid_date_str,
                "listing_date": listing_date_str,
                "auction_quantity": auction_qty_str,
                "min_price": min_price_str,
                "min_bid_quantity": min_bid_qty_str,
                "max_bid_quantity": max_bid_qty_str,
                "margin_percentage": margin_pct_str,
                "processing_fee": fee_str,
                "underwriter": underwriter,
                "award_fee_rate": award_fee_rate,
                "total_award_amount": total_award_amt,
                "status_code": status_code,
                "status_label": status_label,
                "days_left": days_left,
                "countdown_text": countdown_text,
                "estimated_min_bid_value": estimated_min_bid_value,
                "estimated_min_margin": estimated_min_margin,
                "twse_url": TWSE_AUCTION_PAGE_URL,
                "yahoo_url": f"https://tw.stock.yahoo.com/quote/{base_code}",
                "goodinfo_url": f"https://goodinfo.tw/tw/StockBzPerformance.asp?STOCK_ID={base_code}",
                "moneydj_url": f"https://justdata.moneydj.com/z/zc/zca/zca_{base_code}.djhtm",
            }
        )

    priority_map = {"bidding": 0, "upcoming": 1, "waiting_open": 2}
    active_auctions.sort(
        key=lambda x: (
            priority_map.get(x["status_code"], 9),
            x["bid_end_date"],
            x["open_bid_date"],
        )
    )
    return active_auctions


def _strip_html(text: str) -> str:
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def fetch_google_news_for_stock(
    security_name: str, security_code: str, max_items: int = 5
) -> List[Dict[str, str]]:
    """
    透過 Google News RSS 搜尋該競拍股票的最新相關新聞。
    """
    queries = clean_company_search_name(security_name, security_code)
    info = get_underlying_code_and_name(security_name, security_code)
    base_name = info["base_name"]
    base_code = info["base_code"]
    collected_news: List[Dict[str, str]] = []
    seen_titles = set()

    for q in queries:
        if len(collected_news) >= max_items:
            break
        encoded_q = urllib.parse.quote(q)
        rss_url = f"{GOOGLE_NEWS_RSS_URL}?q={encoded_q}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
        try:
            xml_bytes = _http_get(rss_url, timeout=15)
            root = ET.fromstring(xml_bytes)
            items = root.findall(".//item")
            for item in items:
                raw_title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                pub_date_raw = (item.findtext("pubDate") or "").strip()
                source_el = item.find("source")
                source_name = (source_el.text or "").strip() if source_el is not None else ""
                desc_raw = item.findtext("description") or ""

                clean_title = raw_title
                if " - " in raw_title:
                    parts = raw_title.rsplit(" - ", 1)
                    clean_title = parts[0].strip()
                    if not source_name:
                        source_name = parts[1].strip()

                # 確保標題包含該公司名稱或股票代號，且排除非財經之一般語彙干擾
                if (
                    security_name not in clean_title
                    and base_name not in clean_title
                    and base_code not in clean_title
                ):
                    continue
                if "全光譜" in clean_title or "連續光譜" in clean_title or "臺灣光譜" in clean_title:
                    continue

                norm_title = re.sub(r"\W+", "", clean_title)
                if not clean_title or norm_title in seen_titles:
                    continue
                seen_titles.add(norm_title)

                formatted_date = pub_date_raw
                pub_dt_sort = ""
                try:
                    dt = parsedate_to_datetime(pub_date_raw)
                    tw_tz = datetime.timezone(datetime.timedelta(hours=8))
                    dt_tw = dt.astimezone(tw_tz)
                    formatted_date = dt_tw.strftime("%Y/%m/%d %H:%M")
                    pub_dt_sort = dt_tw.isoformat()
                except Exception:
                    pass

                snippet = _strip_html(desc_raw)
                if snippet.startswith(clean_title):
                    snippet = snippet[len(clean_title) :].strip(" -–—")
                if source_name and snippet.endswith(source_name):
                    snippet = snippet[: -len(source_name)].strip(" -–—")

                collected_news.append(
                    {
                        "title": clean_title,
                        "url": link,
                        "source": source_name or "財經新聞",
                        "published_at": formatted_date,
                        "sort_time": pub_dt_sort,
                        "snippet": snippet if snippet and snippet != clean_title else "",
                    }
                )
                if len(collected_news) >= max_items:
                    break
        except Exception as e:
            print(f"[WARN] 搜尋『{q}』新聞時發生錯誤: {e}")

    collected_news.sort(key=lambda x: x.get("sort_time", ""), reverse=True)
    return collected_news[:max_items]


def generate_summary_for_auction(
    auction: Dict[str, Any],
    news_list: List[Dict[str, str]],
    gemini_api_key: Optional[str] = None,
) -> Dict[str, str]:
    """
    產生該檔競拍股票的「競拍重點與新聞整理摘要」。
    """
    sec_name = auction["security_name"]
    sec_code = auction["security_code"]
    issue_type = auction["issue_type"]
    market = auction["market"]
    min_price = auction["min_price"]
    qty = auction["auction_quantity"]
    bid_start = auction["bid_start_date"]
    bid_end = auction["bid_end_date"]
    open_date = auction["open_bid_date"]
    listing_date = auction["listing_date"]
    underwriter = auction["underwriter"]
    min_margin = f"{auction['estimated_min_margin']:,}"

    auction_overview = (
        f"【{sec_name} ({sec_code})】為{market}之「{issue_type}」競拍案，由{underwriter}證券主辦。"
        f"本次競拍數量共 {qty} 張，最低投標底價為 {min_price} 元"
        f"（每標單最低預估保證金加處理費約 NT$ {min_margin} 元）。"
        f"投標期間為 {bid_start} 至 {bid_end}，預計於 {open_date} 開標、{listing_date} 撥券掛牌。"
    )

    if gemini_api_key and news_list:
        ai_summary = _call_gemini_summary(auction, news_list, gemini_api_key)
        if ai_summary:
            return {
                "overview": auction_overview,
                "news_digest": ai_summary,
                "mode": "gemini_ai",
            }

    if not news_list:
        news_digest = f"目前網路上尚無【{sec_name} ({sec_code})】近期的相關新聞報導，建議直接參考公開說明書與證交所競拍公告。"
    else:
        sources = []
        highlights = []
        for idx, item in enumerate(news_list[:4], 1):
            src = item.get("source", "媒體")
            if src not in sources:
                sources.append(src)
            highlights.append(f"({idx}) {item['title']}（{src}）")

        source_str = "、".join(sources[:3])
        news_digest = (
            f"綜合 {source_str} 等共 {len(news_list)} 則近期新聞焦點："
            + "；".join(highlights)
            + "。"
        )

    return {
        "overview": auction_overview,
        "news_digest": news_digest,
        "mode": "extractive",
    }


def _call_gemini_summary(
    auction: Dict[str, Any], news_list: List[Dict[str, str]], api_key: str
) -> Optional[str]:
    """選用功能：若使用者提供 GEMINI_API_KEY，呼叫 Gemini API 產生精煉新聞摘要"""
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.5-flash:generateContent?key={urllib.parse.quote(api_key)}"
    )
    news_text = "\n".join(
        [
            f"- [{n['published_at']}] {n['title']} ({n['source']}) {n.get('snippet', '')}"
            for n in news_list
        ]
    )
    cp = auction.get("company_profile", {})
    prompt = (
        f"請根據以下關於台灣競拍股票「{auction['security_name']} ({auction['security_code']})」"
        f"（{auction['issue_type']}，底價 {auction['min_price']} 元，基本面：{cp.get('summary_text', '')}）"
        f"的最新新聞標題與片段，用繁體中文整理出 120~180 字的重點新聞摘要：\n\n"
        f"{news_text}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 350},
    }
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20, context=_create_ssl_context()) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            candidates = result.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                if parts and parts[0].get("text"):
                    return parts[0]["text"].strip()
    except Exception as e:
        print(f"[WARN] Gemini AI 摘要呼叫失敗，自動切換為預設摘要: {e}")
    return None


def collect_active_auctions_with_news(
    gemini_api_key: Optional[str] = None,
    max_news_per_stock: int = 5,
    only_stocks: bool = True,
) -> Dict[str, Any]:
    """
    一鍵執行：抓取 TWSE 未結束競拍 + 抓取公司介紹/營收規模/利潤率 + 搜尋各檔新聞 + 產生摘要。
    """
    tw_tz = datetime.timezone(datetime.timedelta(hours=8))
    now_tw = datetime.datetime.now(tw_tz)

    auctions = fetch_twse_auctions(reference_date=now_tw.date(), only_stocks=only_stocks)

    for auction in auctions:
        # 1. 抓取公司基本面介紹（行業、業務範圍、營收規模、毛利率/營益率/淨利率/EPS）
        company_profile = fetch_company_profile_and_financials(
            auction["security_name"],
            auction["security_code"],
            market=auction.get("market", ""),
        )
        auction["company_profile"] = company_profile

        # 2. 抓取相關新聞
        news_items = fetch_google_news_for_stock(
            auction["security_name"],
            auction["security_code"],
            max_items=max_news_per_stock,
        )
        auction["news"] = news_items

        # 3. 產生競拍與新聞重點摘要
        summary_info = generate_summary_for_auction(
            auction, news_items, gemini_api_key=gemini_api_key
        )
        auction["summary_overview"] = summary_info["overview"]
        auction["summary_news_digest"] = summary_info["news_digest"]
        auction["summary_mode"] = summary_info["mode"]

    bidding_count = sum(1 for a in auctions if a["status_code"] == "bidding")
    upcoming_count = sum(1 for a in auctions if a["status_code"] == "upcoming")
    waiting_open_count = sum(1 for a in auctions if a["status_code"] == "waiting_open")
    ipo_stock_count = sum(1 for a in auctions if not a["is_cb"])
    cb_count = sum(1 for a in auctions if a["is_cb"])

    return {
        "updated_at": now_tw.strftime("%Y-%m-%d %H:%M:%S (TST)"),
        "reference_date": now_tw.strftime("%Y/%m/%d"),
        "source_url": TWSE_AUCTION_PAGE_URL,
        "stats": {
            "total_active": len(auctions),
            "bidding_count": bidding_count,
            "upcoming_count": upcoming_count,
            "waiting_open_count": waiting_open_count,
            "ipo_stock_count": ipo_stock_count,
            "cb_count": cb_count,
        },
        "auctions": auctions,
    }


if __name__ == "__main__":
    data = collect_active_auctions_with_news()
    print(f"更新時間: {data['updated_at']}，共找到 {data['stats']['total_active']} 檔尚未結束競拍標的")
    for item in data["auctions"]:
        cp = item.get("company_profile", {})
        print(
            f"- [{item['status_label']}] {item['security_name']} ({item['security_code']}) "
            f"| 產業: {cp.get('industry')} | 毛利率: {cp.get('gross_margin')} | 營益率: {cp.get('operating_margin')}"
        )
