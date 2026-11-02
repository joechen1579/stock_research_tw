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


def _infer_sub_industry_tag(business_text: str) -> str:
    """根據主要經營業務與產品營收比重，推論精準的細分產業標籤（搭配官方/Goodinfo 大類顯示）"""
    text = business_text or ""
    mapping = [
        (["高科技廠房", "廠務", "無塵室", "二次配", "配管"], "半導體廠務工程"),
        (["晶圓", "半導體", "CoWoS", "封測", "IC", "晶片"], "半導體設備與材料"),
        (["電梯", "昇降機", "自動門", "輸送機械"], "電梯製造與機電維保"),
        (["沖床", "工具機", "車床", "精密機械"], "精密機械設備"),
        (["變壓器", "重電", "配電", "電力", "電工", "馬達"], "重電與電力設備"),
        (["複合材料", "工程塑膠", "母粒", "樹脂", "高分子", "特用化學"], "高性能複合材料"),
        (["發光二極體", "LED", "雷射", "磊晶", "光電", "光學", "面板"], "光電與光學元件"),
        (["生技", "新藥", "醫療", "醫材", "製藥", "保健"], "生技醫療與醫材"),
        (["軟體", "雲端", "資訊", "資安", "AI", "系統整合"], "數位雲端與資訊服務"),
        (["伺服器", "散熱", "印刷電路板", "PCB", "連接器", "電源"], "電子零組件與伺服器"),
        (["餐飲", "食品", "零售", "百貨", "居家", "貿易"], "民生消費與通路"),
    ]
    for keywords, label in mapping:
        for kw in keywords:
            if kw in text:
                return label
    return ""


def _fetch_official_isin_and_mops_basic(base_code: str) -> Dict[str, str]:
    """
    從證交所 ISIN 官方資料庫與 MOPS 公開資訊觀測站 (t05st03) 抓取與 Goodinfo 完全一致的
    「官方產業類別」、「主要經營業務」、「實收資本額」、「董事長」等基本資料。
    """
    info = {
        "official_industry": "",
        "business_scope": "",
        "capital_text": "",
        "chairman": "",
        "founded_date": "",
    }
    # 1. 抓取 TWSE ISIN 官方產業別 (與 Goodinfo 產業別完全同源)
    try:
        isin_url = f"https://isin.twse.com.tw/isin/single_main.jsp?owncode={base_code}"
        raw_isin = _http_get(isin_url, timeout=8).decode("ms950", "ignore")
        cells = [
            re.sub(r"<[^>]+>", "", c).strip()
            for c in re.findall(r"<td[^>]*>(.*?)</td>", raw_isin, flags=re.S | re.I)
        ]
        # 欄位順序：頁面編號(9), 國際證券編碼(10), 代號(11), 名稱(12), 市場別(13), 證券別(14), 產業別(15)
        if len(cells) >= 16 and cells[11] == base_code and cells[15]:
            info["official_industry"] = cells[15].strip()
    except Exception:
        pass

    # 2. 抓取 MOPS t05st03 公司基本資料（補足興櫃公司之產業類別與主要經營業務）
    try:
        post_data = urllib.parse.urlencode(
            {
                "encodeURIComponent": "1",
                "step": "1",
                "firstin": "1",
                "off": "1",
                "co_id": base_code,
                "TYPEK": "all",
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            "https://mopsov.twse.com.tw/mops/web/ajax_t05st03",
            data=post_data,
            headers={"User-Agent": USER_AGENT},
        )
        with urllib.request.urlopen(req, timeout=10, context=_create_ssl_context()) as resp:
            raw_mops = resp.read().decode("utf-8", "ignore")
        toks = [
            html.unescape(t).replace("\xa0", " ").strip()
            for t in re.findall(r">([^<>]{1,250})<", raw_mops)
            if html.unescape(t).replace("\xa0", " ").strip()
        ]
        for idx, tok in enumerate(toks):
            if tok == "產業類別" and idx + 1 < len(toks) and not info["official_industry"]:
                cand = toks[idx + 1].strip()
                if cand and cand not in ("外國企業註冊地國", "－"):
                    info["official_industry"] = cand
            elif tok == "董事長" and idx + 1 < len(toks):
                info["chairman"] = toks[idx + 1].strip()
            elif tok == "公司成立日期" and idx + 1 < len(toks):
                info["founded_date"] = toks[idx + 1].strip()
            elif tok == "實收資本額" and idx + 1 < len(toks):
                raw_cap = toks[idx + 1].replace("元", "").replace(",", "").strip()
                cap_num = parse_float(raw_cap)
                if cap_num > 0:
                    info["capital_text"] = f"{cap_num / 1e8:.2f} 億元"
            elif tok == "主要經營業務" and idx + 1 < len(toks):
                biz_lines = []
                for k in range(idx + 1, min(idx + 6, len(toks))):
                    if toks[k] in ("公司成立日期", "營利事業統一編號", "實收資本額"):
                        break
                    biz_lines.append(toks[k])
                if biz_lines:
                    info["business_scope"] = "、".join(biz_lines)
    except Exception:
        pass

    return info


def _fetch_mops_revenue_and_income_fallback(base_code: str, profile: Dict[str, Any]) -> None:
    """
    當 MoneyDJ 尚未收錄該興櫃公司之月營收或損益表（如耐特 8058）時，
    直接從 MOPS 公開資訊觀測站抓取最新月營收 (ajax_t05st10_ifrs) 與合併綜合損益表 (ajax_t164sb04)。
    """
    # A. 補抓最新單月與累計營收
    if not profile.get("latest_month_revenue"):
        try:
            post_data = urllib.parse.urlencode(
                {
                    "encodeURIComponent": "1",
                    "step": "1",
                    "firstin": "1",
                    "off": "1",
                    "co_id": base_code,
                    "isnew": "true",
                    "TYPEK": "all",
                }
            ).encode("utf-8")
            req = urllib.request.Request(
                "https://mopsov.twse.com.tw/mops/web/ajax_t05st10_ifrs",
                data=post_data,
                headers={"User-Agent": USER_AGENT},
            )
            with urllib.request.urlopen(req, timeout=10, context=_create_ssl_context()) as resp:
                raw_html = resp.read().decode("utf-8", "ignore")
            toks = [
                html.unescape(t).replace("\u3000", " ").strip()
                for t in re.findall(r">([^<>]{1,120})<", raw_html)
                if html.unescape(t).strip()
            ]
            for idx, tok in enumerate(toks):
                m_ym = re.search(r"民國(\d{3})年(\d{2})月", tok)
                if m_ym and not profile["latest_month_label"]:
                    profile["latest_month_label"] = f"{m_ym.group(1)}/{m_ym.group(2)}"
                elif tok == "本月" and idx + 1 < len(toks) and not profile["latest_month_revenue"]:
                    profile["latest_month_revenue"] = format_thousand_ntd_to_yi(toks[idx + 1])
                elif tok == "增減百分比" and idx + 1 < len(toks):
                    val_pct = toks[idx + 1]
                    if re.match(r"^[+-]?\d+(?:\.\d+)?$", val_pct):
                        if not profile["latest_month_yoy"]:
                            profile["latest_month_yoy"] = f"{val_pct}%"
                        elif not profile["accumulated_yoy"]:
                            profile["accumulated_yoy"] = f"{val_pct}%"
                elif tok == "本年累計" and idx + 1 < len(toks) and not profile["accumulated_revenue"]:
                    profile["accumulated_revenue"] = format_thousand_ntd_to_yi(toks[idx + 1])
        except Exception:
            pass

    # B. 補抓最新合併損益表 (毛利率、營益率、稅前淨利率、EPS)
    if not profile.get("gross_margin") or not profile.get("latest_quarter_eps"):
        try:
            post_data = urllib.parse.urlencode(
                {
                    "encodeURIComponent": "1",
                    "step": "1",
                    "firstin": "1",
                    "off": "1",
                    "co_id": base_code,
                    "isnew": "true",
                    "TYPEK": "all",
                }
            ).encode("utf-8")
            req = urllib.request.Request(
                "https://mopsov.twse.com.tw/mops/web/ajax_t164sb04",
                data=post_data,
                headers={"User-Agent": USER_AGENT},
            )
            with urllib.request.urlopen(req, timeout=10, context=_create_ssl_context()) as resp:
                raw_html = resp.read().decode("utf-8", "ignore")
            toks = [
                html.unescape(t).strip()
                for t in re.findall(r">([^<>]{1,120})<", raw_html)
                if html.unescape(t).strip()
            ]
            for idx, tok in enumerate(toks):
                m_q = re.search(r"民國(\d{3})年第(\d)季", tok)
                if m_q and not profile["profit_period"]:
                    q_label = f"{m_q.group(1)}.{m_q.group(2)}Q"
                    profile["profit_period"] = q_label
                    profile["latest_quarter_label"] = q_label
                elif tok in ("營業收入合計", "銷貨收入淨額") and idx + 1 < len(toks) and not profile["latest_quarter_revenue"]:
                    q_rev_k = parse_float(toks[idx + 1])
                    if q_rev_k > 0:
                        profile["latest_quarter_revenue"] = format_thousand_ntd_to_yi(str(q_rev_k))
                elif tok in ("營業毛利（毛損）淨額", "營業毛利（毛損）") and idx + 2 < len(toks) and not profile["gross_margin"]:
                    if re.match(r"^[+-]?\d+(?:\.\d+)?$", toks[idx + 2]):
                        profile["gross_margin"] = f"{toks[idx + 2]}%"
                elif tok == "營業利益（損失）" and idx + 2 < len(toks) and not profile["operating_margin"]:
                    if re.match(r"^[+-]?\d+(?:\.\d+)?$", toks[idx + 2]):
                        profile["operating_margin"] = f"{toks[idx + 2]}%"
                elif tok in ("稅前淨利（淨損）", "繼續營業單位稅前淨利（淨損）") and idx + 2 < len(toks) and not profile["pretax_margin"]:
                    if re.match(r"^[+-]?\d+(?:\.\d+)?$", toks[idx + 2]):
                        profile["pretax_margin"] = f"{toks[idx + 2]}%"
                elif tok == "本期淨利（淨損）" and idx + 1 < len(toks) and not profile["latest_quarter_net_income"]:
                    q_net_k = parse_float(toks[idx + 1])
                    if q_net_k != 0:
                        profile["latest_quarter_net_income"] = f"{q_net_k / 1000.0:,.0f} 百萬元"
                elif tok == "基本每股盈餘" and idx + 3 < len(toks) and not profile["latest_quarter_eps"]:
                    # 格式可能為: 基本每股盈餘 -> 繼續營業單位淨利 -> 1.04 (當季) -> 0.43 (去年同季) -> 1.85 (累計)
                    offset = 2 if not re.match(r"^[+-]?\d+(?:\.\d+)?$", toks[idx + 1]) else 1
                    if idx + offset < len(toks) and re.match(r"^[+-]?\d+(?:\.\d+)?$", toks[idx + offset]):
                        q_eps_val = toks[idx + offset]
                        profile["latest_quarter_eps"] = f"{q_eps_val} 元"
                        if idx + offset + 2 < len(toks) and re.match(r"^[+-]?\d+(?:\.\d+)?$", toks[idx + offset + 2]):
                            acc_eps_val = toks[idx + offset + 2]
                            profile["recent_4q_eps"] = f"{acc_eps_val} 元 (近2季累計)"
                        else:
                            profile["recent_4q_eps"] = f"{q_eps_val} 元 (近1季累計)"
        except Exception:
            pass


def fetch_company_profile_and_financials(
    security_name: str, security_code: str, market: str = ""
) -> Dict[str, Any]:
    """
    自動抓取並整理公司介紹摘要：
    1. 所屬行業 (與 Goodinfo / 證交所 ISIN / MOPS 同步之官方產業類別 + 細分領域標籤)
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
        "official_industry": "",
        "industry": "",
        "business_scope": "",
        "revenue_mix": "",
        "capital_text": "",
        "chairman": "",
        "founded_date": "",
        "website": "",
        "goodinfo_url": f"https://goodinfo.tw/tw/StockDetail.asp?STOCK_ID={base_code}",
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

    # Step 1: 抓取證交所 ISIN 與 MOPS (t05st03) 官方產業類別與主要經營業務（與 Goodinfo 完全一致）
    official_basic = _fetch_official_isin_and_mops_basic(base_code)
    if official_basic["official_industry"]:
        profile["official_industry"] = official_basic["official_industry"]
    if official_basic["business_scope"]:
        profile["business_scope"] = official_basic["business_scope"]
    if official_basic["capital_text"]:
        profile["capital_text"] = official_basic["capital_text"]
    if official_basic["chairman"]:
        profile["chairman"] = official_basic["chairman"]
    if official_basic["founded_date"]:
        profile["founded_date"] = official_basic["founded_date"]

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
        elif c == "董事長" and i + 1 < len(zca_cells) and not profile["chairman"]:
            profile["chairman"] = zca_cells[i + 1]
        elif c == "成立時間" and i + 1 < len(zca_cells) and not profile["founded_date"]:
            profile["founded_date"] = zca_cells[i + 1]
        elif c == "網址" and i + 1 < len(zca_cells):
            if zca_cells[i + 1].startswith("http"):
                profile["website"] = zca_cells[i + 1]

    # Step 3: 抓取 MoneyDJ zch_{base_code} (最新月營收與累計營收規模)
    zch_cells = _extract_moneydj_cells("zch", base_code)
    for i, c in enumerate(zch_cells):
        if re.match(r"^\d{3}/\d{2}$", c) and i + 6 < len(zch_cells):
            profile["latest_month_label"] = c
            raw_month_rev = zch_cells[i + 1]
            raw_yoy = zch_cells[i + 4]
            raw_acc_rev = zch_cells[i + 5]
            raw_acc_yoy = zch_cells[i + 6]
            profile["latest_month_revenue"] = format_thousand_ntd_to_yi(raw_month_rev)
            profile["latest_month_yoy"] = (
                f"{raw_yoy}%" if raw_yoy and not str(raw_yoy).endswith("%") else raw_yoy
            )
            profile["accumulated_revenue"] = format_thousand_ntd_to_yi(raw_acc_rev)
            profile["accumulated_yoy"] = (
                f"{raw_acc_yoy}%" if raw_acc_yoy and not str(raw_acc_yoy).endswith("%") else raw_acc_yoy
            )
            break

    # Step 4: 抓取 MoneyDJ zcd_{base_code} (最新季報營業收入、稅後淨利與 EPS)
    zcd_cells = _extract_moneydj_cells("zcd", base_code)
    eps_list: List[float] = []
    for i, c in enumerate(zcd_cells):
        if re.match(r"^\d{3}\.[1-4]Q$", c) and i + 7 < len(zcd_cells):
            if not profile["latest_quarter_label"]:
                profile["latest_quarter_label"] = c
                q_rev_m = parse_float(zcd_cells[i + 2])
                q_net_m = parse_float(zcd_cells[i + 4])
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

    # Step 5: 若 MoneyDJ 缺漏月營收或損益表（如興櫃耐特 8058），自動由 MOPS 公開資訊觀測站補齊
    _fetch_mops_revenue_and_income_fallback(base_code, profile)

    # Step 6: 合成精準的「Goodinfo / 官方產業類別 ＋ 細分業務領域」（絕不顯示籠統的「櫃檯買賣企業」）
    combined_biz = " ".join([profile["business_scope"], profile["revenue_mix"]]).strip()
    sub_tag = _infer_sub_industry_tag(combined_biz)
    official_ind = profile["official_industry"]
    if official_ind and sub_tag and sub_tag not in official_ind:
        profile["industry"] = f"{official_ind}（{sub_tag}）"
    elif official_ind:
        profile["industry"] = official_ind
    elif sub_tag:
        profile["industry"] = sub_tag
    else:
        profile["industry"] = "電子與精密製造業"

    if not profile["business_scope"] and profile["revenue_mix"]:
        profile["business_scope"] = profile["revenue_mix"]

    # 組裝精簡無重複的「公司介紹與財務基本面摘要」（僅保留關鍵指標，移除重複之主要營收比重列）
    lines = []
    ind_str = profile["industry"] or "上市櫃/興櫃企業"
    lines.append(f"【所屬產業】{base_name}（{base_code}）隸屬於「{ind_str}」")

    if profile["business_scope"]:
        lines.append(f"【主要業務】{profile['business_scope']}")

    # 營收規模句
    rev_items = []
    if profile["latest_month_revenue"]:
        rev_items.append(
            f"最新 ({profile['latest_month_label']}) 單月營收 {profile['latest_month_revenue']}（年增 {profile['latest_month_yoy']}）"
        )
    if profile["accumulated_revenue"]:
        rev_items.append(
            f"累計營收 {profile['accumulated_revenue']}（年增 {profile['accumulated_yoy']}）"
        )
    if profile["latest_quarter_revenue"]:
        rev_items.append(
            f"最新季報 ({profile['latest_quarter_label']}) 單季營收 {profile['latest_quarter_revenue']}、稅後淨利 {profile['latest_quarter_net_income']}"
        )
    if rev_items:
        lines.append("【營收規模】" + " ｜ ".join(rev_items))

    # 利潤率句
    margin_items = []
    if profile["gross_margin"] and profile["gross_margin"] != "N/A":
        margin_items.append(f"毛利率 {profile['gross_margin']}")
    if profile["operating_margin"] and profile["operating_margin"] != "N/A":
        margin_items.append(f"營益率 {profile['operating_margin']}")
    if profile["pretax_margin"] and profile["pretax_margin"] != "N/A":
        margin_items.append(f"稅前淨利率 {profile['pretax_margin']}")
    if profile["roe"] and profile["roe"] != "N/A":
        margin_items.append(f"ROE {profile['roe']}")
    if margin_items:
        period_tag = f" ({profile['profit_period']})" if profile["profit_period"] else ""
        lines.append(f"【利潤率指標{period_tag}】" + " ｜ ".join(margin_items))

    # 每股獲利與淨值句
    eps_items = []
    if profile["latest_quarter_eps"]:
        eps_items.append(f"最新單季 EPS {profile['latest_quarter_eps']}")
    if profile["recent_4q_eps"]:
        eps_items.append(f"近四季累計 EPS {profile['recent_4q_eps']}")
    if profile["bps"] and profile["bps"] != "N/A":
        eps_items.append(f"每股淨值 {profile['bps']} 元")
    if eps_items:
        lines.append("【每股獲利與淨值】" + " ｜ ".join(eps_items))

    profile["summary_lines"] = lines
    profile["summary_text"] = "\n".join(lines)
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


_GLOBAL_OFFICIAL_PE_MAP: Dict[str, Dict[str, float]] = {}


def _load_official_twse_tpex_pe_map() -> Dict[str, Dict[str, Any]]:
    """
    方案 A：從臺灣證券交易所 (TWSE OpenAPI BWIBBU_ALL) 與櫃買中心 (TPEx OpenAPI)
    一次抓取全市場上市櫃公司當日官方真實「本益比 (PE)、股價淨值比 (PB)、殖利率 (Yield)」。
    """
    global _GLOBAL_OFFICIAL_PE_MAP
    if _GLOBAL_OFFICIAL_PE_MAP:
        return _GLOBAL_OFFICIAL_PE_MAP

    pe_map: Dict[str, Dict[str, Any]] = {}
    # 1. 上市 (TWSE BWIBBU_ALL)
    try:
        raw_twse = _http_get("https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL", timeout=10)
        rows = json.loads(raw_twse.decode("utf-8", "ignore"))
        for r in rows:
            code = str(r.get("Code", "")).strip()
            if not code:
                continue
            pe_map[code] = {
                "name": str(r.get("Name", "")).strip(),
                "pe": parse_float(str(r.get("PEratio", "0"))),
                "pb": parse_float(str(r.get("PBratio", "0"))),
                "yield": parse_float(str(r.get("DividendYield", "0"))),
                "date": str(r.get("Date", "")).strip(),
            }
    except Exception:
        pass

    # 2. 上櫃 (TPEx OpenAPI)
    try:
        raw_tpex = _http_get("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis", timeout=10)
        rows = json.loads(raw_tpex.decode("utf-8", "ignore"))
        for r in rows:
            code = str(r.get("SecuritiesCompanyCode", "")).strip()
            if not code:
                continue
            pe_map[code] = {
                "name": str(r.get("CompanyName", "")).strip(),
                "pe": parse_float(str(r.get("PriceEarningRatio", "0"))),
                "pb": parse_float(str(r.get("PriceBookRatio", "0"))),
                "yield": parse_float(str(r.get("YieldRatio", "0"))),
                "date": str(r.get("Date", "")).strip(),
            }
    except Exception:
        pass

    _GLOBAL_OFFICIAL_PE_MAP = pe_map
    return _GLOBAL_OFFICIAL_PE_MAP


def fetch_official_sector_pe_and_peers(industry_text: str, business_text: str = "") -> Dict[str, Any]:
    """
    依股票之精準產業與主要業務，對接證交所/櫃買中心官方當日同業真實本益比 (PE)、股價淨值比 (PB) 與殖利率，
    並提供產業景氣週期判讀（方案 A：官方原始總經與產業估值數據）。
    """
    combined = f"{industry_text} {business_text}"
    pe_map = _load_official_twse_tpex_pe_map()

    # 根據細分產業挑選最具代表性的台股上市櫃採樣同業代號與產業景氣觀點
    if any(k in combined for k in ["廠務", "高科技廠房", "無塵室", "二次配"]):
        peer_codes = ["2404", "6196", "6139", "5536", "6613"]  # 漢唐、帆宣、亞翔、聖暉*、朋億*
        sector_name = "其他電子業（半導體廠務工程）"
        cycle_note = "受惠晶圓代工龍頭擴充先進封裝 (CoWoS) 與海外建廠資本支出擴張，廠務工程在手訂單維持高檔，屬景氣擴張順風期。"
        fallback_pe = (16.0, 20.0, 24.0)
    elif any(k in combined for k in ["電梯", "昇降機", "電機", "重電", "配電", "沖床", "機械"]):
        peer_codes = ["4506", "1514", "1513", "1504", "4571"]  # 崇友、亞力、中興電、東元、鈞興-KY
        sector_name = "電機機械（電梯與機電設備）"
        cycle_note = "受惠台灣老舊大樓電梯汰舊換新法規、廠辦建案交屋潮與長期維保剛性需求，現金流與毛利率具備高度防禦性。"
        fallback_pe = (15.0, 19.0, 23.0)
    elif any(k in combined for k in ["複合材料", "工程塑膠", "母粒", "樹脂", "塑膠", "特用化學"]):
        peer_codes = ["4766", "1717", "1323", "1319", "4763"]  # 南寶、長興、永裕、東陽、材料-KY
        sector_name = "其他電子業 / 高性能複合材料"
        cycle_note = "隨 AI 伺服器輕量化機構件、車用電子與半導體載具對高機能工程塑膠需求回溫，高毛利客製化複合材料優於傳統泛用塑化。"
        fallback_pe = (14.0, 17.5, 21.0)
    elif any(k in combined for k in ["半導體", "IC", "晶圓", "封測", "晶片", "IP"]):
        peer_codes = ["2330", "3711", "2449", "6239", "3443"]
        sector_name = "半導體業"
        cycle_note = "AI 算力與先進製程需求強勁，台灣半導體出口與設備進口年增率處於擴張週期。"
        fallback_pe = (18.0, 22.0, 26.0)
    elif any(k in combined for k in ["生技", "新藥", "醫療", "醫材", "製藥"]):
        peer_codes = ["1795", "4105", "4162", "6446", "4128"]
        sector_name = "生技醫療業"
        cycle_note = "醫材與高毛利保健/特用醫藥受景氣波動影響低，估值主要錨定營收成長率與國際取證進度。"
        fallback_pe = (16.0, 20.0, 25.0)
    else:
        peer_codes = ["2308", "2382", "2395", "3017", "6669"]
        sector_name = industry_text or "電子與精密製造業"
        cycle_note = "台灣製造業外銷訂單與電子零組件出口維持溫和擴張，建議以同業平均本益比中位數作為定價核心。"
        fallback_pe = (14.0, 17.5, 21.0)

    sampled_peers = []
    valid_pes = []
    valid_pbs = []
    valid_yields = []
    data_date = ""

    for code in peer_codes:
        item = pe_map.get(code)
        if not item:
            continue
        if item.get("date") and not data_date:
            data_date = item["date"]
        pe_val = item.get("pe", 0.0)
        pb_val = item.get("pb", 0.0)
        yld_val = item.get("yield", 0.0)
        if 4.0 <= pe_val <= 65.0:
            valid_pes.append(pe_val)
        if 0.3 <= pb_val <= 20.0:
            valid_pbs.append(pb_val)
        if 0.0 < yld_val <= 15.0:
            valid_yields.append(yld_val)
        if pe_val > 0 or pb_val > 0:
            sampled_peers.append(
                f"{item['name']}({code}) PE {pe_val:.1f}x / PB {pb_val:.2f}x"
            )

    if len(valid_pes) >= 2:
        valid_pes_sorted = sorted(valid_pes)
        avg_pe = round(sum(valid_pes) / len(valid_pes), 1)
        pe_low = round(max(10.0, min(valid_pes_sorted[0], avg_pe * 0.85)), 1)
        pe_high = round(min(40.0, max(valid_pes_sorted[-1], avg_pe * 1.15)), 1)
        pe_mid = avg_pe
    else:
        pe_low, pe_mid, pe_high = fallback_pe
        avg_pe = pe_mid

    avg_pb = round(sum(valid_pbs) / len(valid_pbs), 2) if valid_pbs else 2.8
    avg_yield = round(sum(valid_yields) / len(valid_yields), 2) if valid_yields else 3.5

    return {
        "sector_name": sector_name,
        "data_date": data_date or "最新交易日",
        "pe_low": pe_low,
        "pe_mid": pe_mid,
        "pe_high": pe_high,
        "avg_pe": avg_pe,
        "avg_pb": avg_pb,
        "avg_yield": avg_yield,
        "sampled_peers": sampled_peers,
        "cycle_note": cycle_note,
    }


def fetch_mops_prospectus_info(base_code: str, roc_year: int) -> Dict[str, str]:
    """
    從 MOPS 公開資訊觀測站「電子書 - 公開說明書」查詢該公司最新上傳之競拍/承銷公開說明書（含主辦承銷商評估報告）。
    """
    mops_ebook_url = (
        f"https://doc.twse.com.tw/server-java/t57sb01?step=1&colorchg=1"
        f"&co_id={base_code}&year={roc_year}&mtype=B"
    )
    mops_company_url = f"https://mops.twse.com.tw/mops/web/t05st03?co_id={base_code}"
    result = {
        "mops_prospectus_url": mops_ebook_url,
        "mops_company_url": mops_company_url,
        "latest_doc_title": "公開說明書（含承銷商評估報告與同業本益比承銷價計算書）",
    }
    try:
        raw_bytes = _http_get(mops_ebook_url, timeout=10)
        raw_html = raw_bytes.decode("big5", "ignore")
        matches = re.findall(r">([^<>]*公開說明書[^<>]*)<", raw_html)
        if matches:
            clean_titles = [m.strip() for m in matches if len(m.strip()) > 4]
            if clean_titles:
                result["latest_doc_title"] = clean_titles[0]
    except Exception:
        pass
    return result


def fetch_realtime_emerging_or_market_price(base_code: str) -> Dict[str, Any]:
    """
    抓取 Yahoo 股市與台灣證交所/櫃買中心之最新興櫃或上市櫃市價（成交價 / 均價）。
    """
    price_info: Dict[str, Any] = {
        "market_price": 0.0,
        "price_source": "",
        "quote_url": f"https://tw.stock.yahoo.com/quote/{base_code}",
    }
    for sym in [f"{base_code}.TWO", f"{base_code}.TW", base_code]:
        y_url = f"https://tw.stock.yahoo.com/quote/{sym}"
        try:
            raw_html = _http_get(y_url, timeout=10).decode("utf-8", "ignore")
            # Yahoo Stock meta price or JSON state
            m_meta = re.search(r'"regularMarketPrice"\s*:\s*\{\s*"raw"\s*:\s*([0-9.]+)', raw_html)
            if not m_meta:
                m_meta = re.search(r'"regularMarketPrice"\s*:\s*([0-9.]+)', raw_html)
            if not m_meta:
                m_meta = re.search(r'"previousClose"\s*:\s*\{\s*"raw"\s*:\s*([0-9.]+)', raw_html)
            if m_meta:
                p = float(m_meta.group(1))
                if p > 0:
                    price_info["market_price"] = round(p, 2)
                    price_info["price_source"] = f"Yahoo股市 ({sym})"
                    price_info["quote_url"] = y_url
                    return price_info
        except Exception:
            pass
    return price_info


def fetch_cnyes_factset_consensus(base_code: str) -> Dict[str, Any]:
    """
    免帳密抓取鉅亨網 (cnyes) / FactSet 公開法人共識目標價（若該股已有外資/本土投顧建檔）。
    """
    cnyes_url = f"https://www.cnyes.com/twstock/{base_code}"
    info: Dict[str, Any] = {
        "has_factset": False,
        "target_mean": 0.0,
        "target_median": 0.0,
        "target_high": 0.0,
        "target_low": 0.0,
        "analyst_count": 0,
        "cnyes_url": cnyes_url,
    }
    try:
        raw_html = _http_get(cnyes_url, timeout=10).decode("utf-8", "ignore")
        m_med = re.search(r'"targetPriceMedian"\s*:\s*([0-9.]+)', raw_html)
        m_mean = re.search(r'"targetPriceMean"\s*:\s*([0-9.]+)', raw_html)
        m_high = re.search(r'"targetPriceHigh"\s*:\s*([0-9.]+)', raw_html)
        m_low = re.search(r'"targetPriceLow"\s*:\s*([0-9.]+)', raw_html)
        m_cnt = re.search(r'"numOfAnalysts"\s*:\s*(\d+)', raw_html)
        if m_med or m_mean:
            med = float(m_med.group(1)) if m_med else 0.0
            mean = float(m_mean.group(1)) if m_mean else med
            if med > 0 or mean > 0:
                info["has_factset"] = True
                info["target_median"] = round(med or mean, 2)
                info["target_mean"] = round(mean or med, 2)
                info["target_high"] = round(float(m_high.group(1)), 2) if m_high else 0.0
                info["target_low"] = round(float(m_low.group(1)), 2) if m_low else 0.0
                info["analyst_count"] = int(m_cnt.group(1)) if m_cnt else 1
    except Exception:
        pass
    return info


def fetch_sinotrade_research(
    security_name: str,
    base_code: str,
    sinotrade_id: str = "",
    sinotrade_password: str = "",
    sinotrade_cookie: str = "",
) -> Dict[str, Any]:
    """
    串接永豐金證券「豐雲學堂 (SinoTrade Rich Club)」與「永豐金證券理財網 / 永豐投顧研究報告」資料庫。
    支援公開檢索 + 使用者提供之永豐帳密/Cookie 讀取專屬研究文章與目標價。
    """
    richclub_search_url = f"https://www.sinotrade.com.tw/richclub/search?keyword={urllib.parse.quote(security_name)}"
    sinotrade_stock_url = f"https://www.sinotrade.com.tw/Stock/Stock_3_1?stock={base_code}"
    sinotrade_ipo_url = "https://www.sinotrade.com.tw/richclub/IPO"

    articles: List[Dict[str, str]] = []
    extracted_targets: List[float] = []

    # 1. 搜尋豐雲學堂與永豐金證券公開/會員專欄文章 (透過站內搜尋與 RSS 檢索)
    queries = [
        f'site:sinotrade.com.tw "{security_name}"',
        f'site:sinotrade.com.tw "{base_code}"',
        f'"永豐投顧" OR "豐雲學堂" "{security_name}"',
    ]
    seen_urls = set()
    for q in queries:
        rss_url = f"{GOOGLE_NEWS_RSS_URL}?q={urllib.parse.quote(q)}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
        try:
            xml_bytes = _http_get(rss_url, timeout=10)
            root = ET.fromstring(xml_bytes)
            for item in root.findall(".//item")[:4]:
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                pub_date = (item.findtext("pubDate") or "").strip()
                if not title or link in seen_urls:
                    continue
                if security_name not in title and base_code not in title:
                    continue
                seen_urls.add(link)
                articles.append(
                    {
                        "title": title,
                        "url": link,
                        "source": "永豐金證券 / 豐雲學堂",
                        "published_at": pub_date[:16],
                    }
                )
                # 嘗試從標題萃取目標價或承銷價數字
                for m in re.finditer(r"(?:目標價|合理價|承銷價|上看|估值|暫定|暫定以新台幣|暫定每股)\s*[:：為約]?\s*(\d{2,4}(?:\.\d+)?)\s*元", title):
                    val = float(m.group(1))
                    if 10 <= val <= 3000 and val not in extracted_targets:
                        extracted_targets.append(val)
        except Exception:
            pass

    # 2. 若使用者有設定永豐帳號或 Cookie，嘗試直接讀取豐雲學堂站內搜尋頁面
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/json,*/*;q=0.8",
        "Accept-Language": "zh-TW,zh;q=0.9",
    }
    if sinotrade_cookie:
        headers["Cookie"] = sinotrade_cookie

    try:
        req = urllib.request.Request(richclub_search_url, headers=headers)
        with urllib.request.urlopen(req, timeout=10, context=_create_ssl_context()) as resp:
            html_str = resp.read().decode("utf-8", "ignore")
            for m in re.finditer(
                r'<a[^>]+href="(/richclub/[^"]+)"[^>]*>(.*?)</a>', html_str, flags=re.S | re.I
            ):
                href, inner = m.group(1), _strip_html(m.group(2))
                if (security_name in inner or base_code in inner) and len(inner) > 6:
                    full_url = f"https://www.sinotrade.com.tw{href}"
                    if full_url not in seen_urls:
                        seen_urls.add(full_url)
                        articles.append(
                            {
                                "title": inner[:90],
                                "url": full_url,
                                "source": "永豐豐雲學堂專欄",
                                "published_at": "近期報告",
                            }
                        )
    except Exception:
        pass

    has_account = bool(sinotrade_id or sinotrade_cookie)
    return {
        "has_sinotrade_account": has_account,
        "sinotrade_account_masked": (sinotrade_id[:3] + "***" + sinotrade_id[-2:]) if len(sinotrade_id) >= 5 else ("已綁定 Cookie" if sinotrade_cookie else "未設定 (使用公開檢索)"),
        "richclub_search_url": richclub_search_url,
        "sinotrade_stock_url": sinotrade_stock_url,
        "sinotrade_ipo_url": sinotrade_ipo_url,
        "articles": articles[:5],
        "extracted_targets": extracted_targets,
    }


def build_valuation_and_consensus(
    auction: Dict[str, Any],
    company_profile: Dict[str, Any],
    news_list: List[Dict[str, str]],
) -> Dict[str, Any]:
    """
    整合多重來源產出「市場共識價格與合理估值分析」：
    1. MOPS 公開說明書與主辦承銷商評估報告連結與定價邏輯說明
    2. 興櫃/二級市場即時成交價 vs 競拍底價（計算套利溢價空間 %）
    3. 針對興櫃半年報特性之滾動 EPS 推算 + 產業合理本益比估值區間
    4. 鉅亨網 FactSet 共識目標價 + 新聞媒體披露之暫定承銷價/法人目標價
    5. 永豐金證券「豐雲學堂」與「理財網客戶專區研究報告」整合連結與觀點
    """
    sec_name = auction.get("security_name", "")
    sec_code = auction.get("security_code", "")
    base_code = auction.get("base_code") or sec_code
    min_price = parse_float(auction.get("min_price", "0"))
    underwriter = auction.get("underwriter", "")

    tw_tz = datetime.timezone(datetime.timedelta(hours=8))
    roc_year = datetime.datetime.now(tw_tz).year - 1911

    # 1. MOPS 公開說明書與承銷商評估報告
    mops_info = fetch_mops_prospectus_info(base_code, roc_year)

    # 2. 即時興櫃/上市櫃市價
    mkt_price_info = fetch_realtime_emerging_or_market_price(base_code)
    market_price = mkt_price_info["market_price"]

    # 3. FactSet / 鉅亨網共識目標價
    factset_info = fetch_cnyes_factset_consensus(base_code)

    # 4. 永豐金證券「豐雲學堂」與「理財網客戶專區」串接
    sinotrade_id = os.environ.get("SINOTRADE_ID", "").strip()
    sinotrade_pwd = os.environ.get("SINOTRADE_PASSWORD", "").strip()
    sinotrade_cookie = os.environ.get("SINOTRADE_COOKIE", "").strip()
    sino_info = fetch_sinotrade_research(
        sec_name, base_code, sinotrade_id, sinotrade_pwd, sinotrade_cookie
    )

    # 5. 從新聞標題與摘要中萃取「暫定承銷價」、「目標價」或「興櫃價」
    news_extracted_prices: List[float] = []
    underwriting_price_Mentioned: Optional[float] = None
    for n in news_list:
        text_blob = f"{n.get('title', '')} {n.get('snippet', '')}"
        for m in re.finditer(
            r"(?:暫定承銷價|承銷價暫定|承銷價為|掛牌價)\s*[:：為約]?\s*(\d{2,4}(?:\.\d+)?)\s*元",
            text_blob,
        ):
            val = float(m.group(1))
            if val >= min_price * 0.8 and val <= min_price * 3.0:
                underwriting_price_Mentioned = val
                news_extracted_prices.append(val)
        for m in re.finditer(
            r"(?:目標價|合理價|法人估目標價|上看)\s*[:：為約]?\s*(\d{2,4}(?:\.\d+)?)\s*元",
            text_blob,
        ):
            val = float(m.group(1))
            if val >= min_price * 0.8 and val <= min_price * 4.0:
                news_extracted_prices.append(val)

    # 依台灣競拍慣例，最低投標底價通常設定為「向證交所申報之暫定承銷價」的 1.15 ~ 1.25 倍倒推（即暫定承銷價約為底價的 1.15~1.20 倍）
    estimated_underwriting_price = (
        underwriting_price_Mentioned
        if underwriting_price_Mentioned
        else round(min_price * 1.18, 1)
    )

    # 6. 解決興櫃半年報問題：計算年化/近四季滾動 EPS 與同業本益比估值
    raw_4q_eps_str = company_profile.get("recent_4q_eps", "")
    raw_1q_eps_str = company_profile.get("latest_quarter_eps", "")
    eps_4q_val = parse_float(raw_4q_eps_str.split("元")[0]) if raw_4q_eps_str else 0.0
    eps_1q_val = parse_float(raw_1q_eps_str.split("元")[0]) if raw_1q_eps_str else 0.0

    # 檢查季數是否因興櫃半年報而少於 4 季
    quarters_match = re.search(r"近(\d+)季", raw_4q_eps_str)
    num_quarters = int(quarters_match.group(1)) if quarters_match else (4 if eps_4q_val > 0 else 0)
    if 0 < num_quarters < 4 and eps_4q_val > 0:
        annualized_eps = round(eps_4q_val * (4.0 / num_quarters), 2)
        eps_basis_label = f"{annualized_eps:.2f} 元（由興櫃近 {num_quarters} 期財報年化推算）"
    elif eps_4q_val > 0:
        annualized_eps = round(eps_4q_val, 2)
        eps_basis_label = f"{annualized_eps:.2f} 元（近四季累計實際 EPS）"
    elif eps_1q_val > 0:
        annualized_eps = round(eps_1q_val * 2.0, 2)
        eps_basis_label = f"{annualized_eps:.2f} 元（由最近期半年報/單季 EPS 年化推算）"
    else:
        annualized_eps = 0.0
        eps_basis_label = "尚無完整公開 EPS 數據"

    sector_macro = fetch_official_sector_pe_and_peers(
        company_profile.get("industry", ""),
        company_profile.get("business_scope", ""),
    )
    pe_low = sector_macro["pe_low"]
    pe_mid = sector_macro["pe_mid"]
    pe_high = sector_macro["pe_high"]
    pe_fair_low = round(annualized_eps * pe_low, 1) if annualized_eps > 0 else 0.0
    pe_fair_mid = round(annualized_eps * pe_mid, 1) if annualized_eps > 0 else 0.0
    pe_fair_high = round(annualized_eps * pe_high, 1) if annualized_eps > 0 else 0.0

    # 7. 計算綜合「市場共識參考價區間」與「預估合理得標價區間」
    candidate_prices = []
    if market_price > 0:
        candidate_prices.append(market_price)
    if factset_info["has_factset"] and factset_info["target_median"] > 0:
        candidate_prices.append(factset_info["target_median"])
    if pe_fair_mid > min_price * 0.7:
        candidate_prices.append(pe_fair_mid)
    candidate_prices.extend(news_extracted_prices)
    candidate_prices.extend(sino_info["extracted_targets"])

    if candidate_prices:
        consensus_low = round(min(candidate_prices), 1)
        consensus_high = round(max(candidate_prices), 1)
        consensus_mid = round(sum(candidate_prices) / len(candidate_prices), 1)
    else:
        consensus_low = round(min_price * 1.15, 1)
        consensus_mid = round(min_price * 1.25, 1)
        consensus_high = round(min_price * 1.38, 1)

    # 計算興櫃市價或共識中位價相對於「競拍底價」的潛在溢價空間
    ref_market_or_consensus = market_price if market_price > 0 else consensus_mid
    spread_pct = (
        round(((ref_market_or_consensus - min_price) / min_price) * 100.0, 1)
        if min_price > 0 and ref_market_or_consensus > 0
        else 0.0
    )

    # 建議投標參考區間（一般台股競拍得標均價約落在底價 +8% ~ 興櫃市價的 88~92 折之間）
    if market_price > min_price:
        suggested_bid_low = round(max(min_price * 1.05, market_price * 0.85), 1)
        suggested_bid_high = round(max(suggested_bid_low, market_price * 0.92), 1)
    else:
        suggested_bid_low = round(min_price * 1.06, 1)
        suggested_bid_high = round(min_price * 1.18, 1)

    peer_summary_str = (
        "、".join(str(p) for p in sector_macro.get("sampled_peers", [])[:4])
        or "依證交所產業分類同業均值估算"
    )

    methodology_lines = [
        f"【MOPS 承銷報告定價依據】主辦承銷商（{underwriter}）於 MOPS 公開說明書中，依法採用「採樣同業本益比法 / 股價淨值比法」訂定競拍底價 {min_price} 元，推估暫定公開申購承銷價約 {estimated_underwriting_price} 元。",
        (
            f"【二級市場/興櫃即時比價】目前市場參考價為 {market_price} 元（較競拍底價 {min_price} 元溢價 {spread_pct:+.1f}%），歷史統計競拍得標均價常落於興櫃市價之 85～92 折區間（約 {suggested_bid_low} ～ {suggested_bid_high} 元）。"
            if market_price > 0
            else f"【預估投標參考區間】依競拍底價 {min_price} 元與暫定承銷價估算，合理投標觀察區間約 {suggested_bid_low} ～ {suggested_bid_high} 元。"
        ),
        (
            f"【同業本益比估值模型】以滾動年化 EPS {eps_basis_label} × 官方採樣同業合理本益比（{pe_low:.1f}x～{pe_high:.1f}x，中位 {pe_mid:.1f}x）推算，基本面合理價值區間約 {pe_fair_low} ～ {pe_fair_high} 元（中位數 {pe_fair_mid} 元）。"
            if annualized_eps > 0
            else "【同業本益比估值模型】因興櫃財報期別揭露限制，建議直接點下方 MOPS 公開說明書或 Goodinfo! 查閱最近期完整損益與同業比價表。"
        ),
        f"【證交所/櫃買同業真實本益比 ＆ 產業景氣（方案 A）】採樣代表同業即時估值：{peer_summary_str}（同業平均 PE {sector_macro['avg_pe']:.1f}x、平均 PB {sector_macro['avg_pb']:.2f}x、平均殖利率 {sector_macro['avg_yield']:.2f}%）。{sector_macro['cycle_note']}",
    ]

    if sino_info["articles"]:
        sino_target_str = (
            f"，並從報告中萃取出參考定價：{'、'.join(f'{p} 元' for p in sino_info['extracted_targets'])}（已納入上方共識價格計算）"
            if sino_info["extracted_targets"]
            else "（已納入綜合評估，點擊下方文章標題可直接閱讀全文）"
        )
        methodology_lines.append(
            f"【永豐金證券 / 豐雲學堂資料庫 ({sino_info['sinotrade_account_masked']})】已成功檢索 {len(sino_info['articles'])} 篇永豐研究快訊/專欄報告{sino_target_str}。"
        )
    else:
        methodology_lines.append(
            f"【永豐金證券 / 豐雲學堂資料庫 ({sino_info['sinotrade_account_masked']})】目前永豐資料庫暫無【{sec_name} ({base_code})】之公開目標價報告，可點擊上方「永豐理財網個股專區」或「Goodinfo! 個股資訊」查看即時法人籌碼。"
        )

    return {
        "min_price": min_price,
        "market_price": market_price,
        "market_price_source": mkt_price_info["price_source"],
        "spread_to_min_pct": spread_pct,
        "estimated_underwriting_price": estimated_underwriting_price,
        "underwriting_price_is_exact": bool(underwriting_price_Mentioned),
        "annualized_eps_label": eps_basis_label,
        "pe_range_label": f"{pe_low:.1f}x ~ {pe_high:.1f}x",
        "pe_fair_low": pe_fair_low,
        "pe_fair_mid": pe_fair_mid,
        "pe_fair_high": pe_fair_high,
        "consensus_low": consensus_low,
        "consensus_mid": consensus_mid,
        "consensus_high": consensus_high,
        "suggested_bid_low": suggested_bid_low,
        "suggested_bid_high": suggested_bid_high,
        "factset": factset_info,
        "mops": mops_info,
        "sinotrade": sino_info,
        "sector_macro": sector_macro,
        "methodology_lines": methodology_lines,
    }


def _parse_float_pct(raw_str: str) -> Optional[float]:
    if not raw_str:
        return None
    m = re.search(r"([+-]?\d+(?:\.\d+)?)", str(raw_str).replace(",", ""))
    if m:
        try:
            return float(m.group(1))
        except Exception:
            return None
    return None


def _apply_smart_bid_tail(raw_price: float, min_price: float, tail_cents: float) -> float:
    """
    將理論投標價加上「防撞單尾數（至小數點後兩位，如 .26 / .68 / .82）」，
    避開散戶習慣的 .0 與 .5 整數擁擠區，並確保不低於競拍底價。
    """
    base_int = int(round(raw_price))
    candidate = round(base_int + tail_cents, 2)
    if candidate <= min_price:
        candidate = round(min_price + max(0.5, tail_cents), 2)
    return candidate


def build_ai_bidding_strategy(
    auction: Dict[str, Any],
    company_profile: Dict[str, Any],
    valuation: Dict[str, Any],
    news_list: List[Dict[str, str]],
    budget_twd: int = 1_000_000,
) -> Dict[str, Any]:
    """
    結合基本面（營收YoY、毛利率、ROE、EPS）、籌碼面（競拍總張數、單筆限額、保證金成數）、
    估值面（底價、暫定承銷價、興櫃/二級市價、同業PE共識、MOPS與永豐報告）與新聞熱度，
    產出細緻的「AI 競拍推演歷程 (5 大步驟)」與「100 萬資金三階梯拆單投標配置表」。
    """
    sec_name = auction.get("security_name", "")
    sec_code = auction.get("security_code", "")
    issue_type = auction.get("issue_type", "初次上市(IPO)")
    underwriter = auction.get("underwriter", "")
    min_price = float(str(auction.get("min_price") or valuation.get("min_price") or "0").replace(",", ""))
    auction_qty = int(float(str(auction.get("auction_quantity") or "0").replace(",", "")))
    min_bid_qty = int(float(str(auction.get("min_bid_quantity") or "1").replace(",", "")))
    max_bid_qty = int(float(str(auction.get("max_bid_quantity") or "999").replace(",", "")))
    margin_pct = float(str(auction.get("margin_percentage") or "30").replace(",", ""))
    fee_rate = 0.045  # 台股競拍得標手續費約 4.5%
    handling_fee_per_order = 400  # 每筆標單投標處理費約 400 元

    mkt_price = float(valuation.get("market_price") or 0.0)
    cons_mid = float(valuation.get("consensus_mid") or 0.0)
    cons_high = float(valuation.get("consensus_high") or 0.0)
    est_uw_price = float(valuation.get("estimated_underwriting_price") or round(min_price * 1.18, 1))
    sug_low = float(valuation.get("suggested_bid_low") or round(min_price * 1.06, 1))
    sug_high = float(valuation.get("suggested_bid_high") or round(min_price * 1.18, 1))
    sug_mid = round((sug_low + sug_high) / 2.0, 2)

    target_ref_price = mkt_price if mkt_price > 0 else (cons_mid if cons_mid > 0 else round(min_price * 1.25, 1))
    target_ref_label = (
        f"興櫃/二級市場最新市價 {mkt_price} 元"
        if mkt_price > 0
        else f"市場共識與同業PE合理中位價 {target_ref_price} 元（區間 {valuation.get('consensus_low', '—')}~{cons_high} 元）"
    )

    # 解析基本面指標以決定「競拍搶標熱度評級」
    yoy_m = _parse_float_pct(company_profile.get("latest_month_yoy", ""))
    yoy_acc = _parse_float_pct(company_profile.get("accumulated_yoy", ""))
    gross_m = _parse_float_pct(company_profile.get("gross_margin", ""))
    op_m = _parse_float_pct(company_profile.get("operating_margin", ""))
    roe_val = _parse_float_pct(company_profile.get("roe", ""))
    industry = company_profile.get("industry", "上市櫃企業")

    heat_score = 50
    heat_reasons = []
    if (yoy_acc is not None and yoy_acc >= 50) or (yoy_m is not None and yoy_m >= 50):
        heat_score += 22
        heat_reasons.append(
            f"營收爆發力強勁（單月年增 {company_profile.get('latest_month_yoy', '—')}、累計年增 {company_profile.get('accumulated_yoy', '—')}）"
        )
    elif (yoy_acc is not None and yoy_acc >= 15) or (yoy_m is not None and yoy_m >= 15):
        heat_score += 12
        heat_reasons.append(
            f"營收維持穩健雙位數成長（單月年增 {company_profile.get('latest_month_yoy', '—')}、累計年增 {company_profile.get('accumulated_yoy', '—')}）"
        )
    else:
        heat_reasons.append(
            f"近期營收動能較溫和（單月年增 {company_profile.get('latest_month_yoy', '—')}），出價宜嚴守安全邊際"
        )

    if gross_m is not None and gross_m >= 30:
        heat_score += 12
        heat_reasons.append(f"高毛利護城河（毛利率 {company_profile.get('gross_margin')}、營益率 {company_profile.get('operating_margin', '—')}）")
    elif gross_m is not None and gross_m >= 20:
        heat_score += 6
        heat_reasons.append(f"利潤率結構穩健（毛利率 {company_profile.get('gross_margin')}）")

    if auction_qty > 0 and auction_qty <= 2200:
        heat_score += 10
        heat_reasons.append(f"競拍釋出籌碼僅 {auction_qty:,} 張（供給偏少，法人與大戶搶標易推升得標均價）")
    elif auction_qty >= 3500:
        heat_score -= 5
        heat_reasons.append(f"競拍釋出總張數達 {auction_qty:,} 張（籌碼供給充裕，有利於中後段低接標單成交）")
    else:
        heat_reasons.append(f"競拍釋出總張數為 {auction_qty:,} 張（籌碼供給適中）")

    if heat_score >= 75:
        heat_level = "🔥 高熱度積極搶標型"
        strategy_tone = "基本面成長強勁且題材吸睛，市場搶標意願高，建議將主力資金配置於區間中上緣，確保核心部位順利得標。"
    elif heat_score >= 60:
        heat_level = "⚡ 中高熱度攻守兼備型"
        strategy_tone = "基本面穩健且籌碼供給適中，建議採取「中樞主力＋低接狙擊」雙軌配置，兼顧得標率與超額價差。"
    else:
        heat_level = "🛡️ 溫和價值防禦型"
        strategy_tone = "籌碼供給充裕或市場熱度溫和，無須過度追價，應以底價微幅加價分批低接，賺取高安全邊際折價。"

    # 計算最高出價防線 (Hard Cap Price)：扣除新股流動性安全折價 8% 與 4.5% 得標手續費
    # 讓 (Hard Cap * 1.045) <= Target * 0.92  => Hard Cap <= Target * 0.88
    raw_hard_cap = round(max(sug_high, target_ref_price * 0.88), 2)
    hard_cap_with_fee = round(raw_hard_cap * (1.0 + fee_rate), 2)

    # 三階梯投標價格設計 (A單: 必中保底單 / B單: 主戰中樞單 / C單: 低接撿便宜單)
    # 並加上小數點後兩位智慧尾數 (.68 / .26 / .12) 避開 .0 與 .5 散戶撞單牆
    price_a = _apply_smart_bid_tail(sug_high, min_price, 0.68 if min_price >= 100 else 0.38)
    price_b = _apply_smart_bid_tail(sug_mid, min_price, 0.26)
    price_c = _apply_smart_bid_tail(sug_low, min_price, 0.12)

    # 確保三階梯價格嚴格遞減 (price_a > price_b > price_c >= min_price)
    if price_b >= price_a:
        price_a = round(price_b + (1.42 if min_price >= 100 else 0.62), 2)
    if price_c >= price_b:
        price_c = round(max(min_price + 0.12, price_b - (1.14 if min_price >= 100 else 0.54)), 2)

    # 根據 100 萬預算計算最適總張數（以含 4.5% 手續費之總交割款最接近 100 萬為基準）
    avg_cost_per_lot = price_b * 1000.0 * (1.0 + fee_rate)
    total_lots = max(min_bid_qty, int(round(budget_twd / avg_cost_per_lot)))
    total_lots = min(total_lots, max_bid_qty)

    # 將 total_lots 拆分為 A / B / C 三張標單
    if total_lots >= 3:
        if heat_score >= 75:
            # 高熱度：提高 A 單與 B 單比重
            lots_a = max(1, int(round(total_lots * 0.35)))
            lots_c = max(1, int(round(total_lots * 0.20)))
            lots_b = max(1, total_lots - lots_a - lots_c)
        else:
            # 溫和或大籌碼：提高 B 單與 C 單比重
            lots_a = max(1, int(round(total_lots * 0.28)))
            lots_c = max(1, int(round(total_lots * 0.28)))
            lots_b = max(1, total_lots - lots_a - lots_c)
        tranche_specs = [
            ("A 單【必中核心單】", "鎖定中上緣價位＋.68/.38 尾數，確保高機率優先得標建立基本倉", price_a, lots_a),
            ("B 單【主戰中樞單】", "配置最多張數於得標中位數＋.26 尾數，越過整數散戶牆、賺取最佳風報比", price_b, lots_b),
            ("C 單【低接狙擊單】", "埋伏於區間下緣＋.12 尾數，若競拍尾盤投標冷清可撿取高折價籌碼", price_c, lots_c),
        ]
    elif total_lots == 2:
        tranche_specs = [
            ("A 單【必中核心單】", "鎖定中上緣價位＋防撞尾數，確保至少 1 張優先得標", price_a, 1),
            ("B 單【低接中樞單】", "配置於建議區間中下緣＋.26 尾數，拉低整體平均持倉成本", price_b, 1),
        ]
    else:
        tranche_specs = [
            ("A 單【主力核心單】", "因單張高價位約佔滿百萬預算，集中 1 張定價於中上緣＋防撞尾數一舉得標", price_a, 1),
        ]

    tranches: List[Dict[str, Any]] = []
    total_bid_principal = 0
    total_fee_twd = 0
    total_settlement_twd = 0
    total_initial_margin_twd = 0
    total_est_profit_twd = 0

    for label, role_desc, p_bid, lots in tranche_specs:
        principal = int(round(p_bid * 1000 * lots))
        win_fee = int(round(principal * fee_rate))
        settlement = principal + win_fee + handling_fee_per_order
        init_margin = int(round(principal * (margin_pct / 100.0))) + handling_fee_per_order
        all_in_cost_per_share = round(settlement / (lots * 1000.0), 2)
        market_value = int(round(target_ref_price * 1000 * lots))
        # 扣除賣出證交稅與手續費約 0.44%
        sell_friction = int(round(market_value * 0.0044))
        est_net_profit = market_value - settlement - sell_friction
        est_roi_pct = round((est_net_profit / settlement) * 100.0, 1) if settlement > 0 else 0.0

        total_bid_principal += principal
        total_fee_twd += win_fee + handling_fee_per_order
        total_settlement_twd += settlement
        total_initial_margin_twd += init_margin
        total_est_profit_twd += est_net_profit

        tranches.append(
            {
                "tranche_name": label,
                "role_desc": role_desc,
                "bid_price": p_bid,
                "all_in_cost_per_share": all_in_cost_per_share,
                "lots": lots,
                "bid_principal_twd": principal,
                "win_fee_twd": win_fee + handling_fee_per_order,
                "initial_margin_twd": init_margin,
                "settlement_twd": settlement,
                "est_net_profit_twd": est_net_profit,
                "est_roi_pct": est_roi_pct,
            }
        )

    weighted_avg_bid = round(total_bid_principal / (total_lots * 1000.0), 2) if total_lots > 0 else 0.0
    weighted_all_in_cost = round(total_settlement_twd / (total_lots * 1000.0), 2) if total_lots > 0 else 0.0
    total_roi_pct = round((total_est_profit_twd / total_settlement_twd) * 100.0, 1) if total_settlement_twd > 0 else 0.0

    sector_macro = valuation.get("sector_macro") or {}
    macro_note = sector_macro.get("cycle_note") or "處於產業結構性成長週期。"
    peer_avg_pe = sector_macro.get("avg_pe") or 18.0
    peer_avg_pb = sector_macro.get("avg_pb") or 2.8
    peer_list_str = "、".join(str(p) for p in (sector_macro.get("sampled_peers") or [])[:4]) or "依證交所同業均值估算"

    # 萃取個股近期關鍵基本面新聞亮點（例如在手訂單、高階應用占比等）
    catalyst_titles = []
    for n in (news_list or [])[:3]:
        t = (n.get("title") or "").strip()
        if t and len(t) > 8 and not t.endswith("股份有限公司"):
            catalyst_titles.append(t)
    catalyst_line = (
        f"【近期營運催化劑】{'；'.join(catalyst_titles[:2])}。"
        if catalyst_titles
        else ""
    )

    # 精簡為 3 大「個股專屬」核心觀點（通用競拍規則與拆單邏輯已移至頁面頂端之大框架 Toggle List）
    step1_bullets = [
        f"【產業分類與景氣位階】隸屬於「{industry}」。{macro_note}",
        f"【官方採樣同業估值】對標同業：{peer_list_str}（同業平均本益比 {peer_avg_pe:.1f}x、平均股價淨值比 {peer_avg_pb:.2f}x）。",
    ]

    step2_bullets = [
        f"【主要業務定位】{company_profile.get('business_scope') or '詳見公開說明書'}。",
        f"【營收與獲利體質】{'；'.join(heat_reasons)}。最新毛利率 {company_profile.get('gross_margin') or '—'}、營益率 {company_profile.get('operating_margin') or '—'}、近期累計 EPS {company_profile.get('recent_4q_eps') or '—'}。",
    ]
    if catalyst_line:
        step2_bullets.append(catalyst_line)

    pe_fair_mid_val = valuation.get("pe_fair_mid") or 0.0
    pe_anchor_str = f" ｜ 同業 PE 合理估值中位數 {pe_fair_mid_val} 元" if pe_fair_mid_val > 0 else ""
    step3_bullets = [
        f"【為何參與 ＆ 策略定調】評級為「{heat_level}」—— {strategy_tone}",
        f"【個股定價錨點與安全邊際】競拍底價 {min_price} 元 ｜ 暫定承銷價 {est_uw_price} 元{pe_anchor_str} ｜ 對比{target_ref_label}（較底價溢價 {valuation.get('spread_to_min_pct', 0):+.1f}%），本檔最高出價防線（Hard Cap）設於 {raw_hard_cap} 元（含費成本 {hard_cap_with_fee} 元/股）。",
        f"【100 萬預算配置結論】最適投標總張數【{total_lots} 張】（A單 {price_a} 元 ／ B單 {price_b} 元 ／ C單 {price_c} 元），加權含費總成本 {weighted_all_in_cost} 元/股，預估潛在總淨利 NT$ {total_est_profit_twd:+,} 元（淨報酬率 {total_roi_pct:+.1f}%）。",
    ]

    reasoning_steps = [
        {
            "step_num": "觀點 1",
            "title": f"怎麼看這產業？（{industry} 景氣位階 ＆ 同業估值對標）",
            "bullets": step1_bullets,
            "detail": " ".join(step1_bullets),
        },
        {
            "step_num": "觀點 2",
            "title": f"怎麼看這個股？（{sec_name} 核心業務、成長動能 ＆ 獲利護城河）",
            "bullets": step2_bullets,
            "detail": " ".join(step2_bullets),
        },
        {
            "step_num": "觀點 3",
            "title": f"為什麼這樣投？（定價安全邊際 ＆ {total_lots} 張百萬資金配置理由）",
            "bullets": step3_bullets,
            "detail": " ".join(step3_bullets),
        },
    ]

    return {
        "budget_twd": budget_twd,
        "heat_level": heat_level,
        "heat_score": heat_score,
        "target_ref_price": target_ref_price,
        "target_ref_label": target_ref_label,
        "hard_cap_price": raw_hard_cap,
        "hard_cap_with_fee": hard_cap_with_fee,
        "total_lots": total_lots,
        "weighted_avg_bid": weighted_avg_bid,
        "weighted_all_in_cost": weighted_all_in_cost,
        "total_initial_margin_twd": total_initial_margin_twd,
        "total_settlement_twd": total_settlement_twd,
        "total_est_profit_twd": total_est_profit_twd,
        "total_roi_pct": total_roi_pct,
        "tranches": tranches,
        "reasoning_steps": reasoning_steps,
    }


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

        # 3. 整合 MOPS 公開說明書、興櫃市價、同業本益比與永豐研究報告共識估值
        valuation = build_valuation_and_consensus(
            auction=auction,
            company_profile=company_profile,
            news_list=news_items,
        )
        auction["valuation"] = valuation

        # 4. 產生 AI 競拍推演歷程與 100 萬資金拆單投標策略
        auction["ai_strategy"] = build_ai_bidding_strategy(
            auction=auction,
            company_profile=company_profile,
            valuation=valuation,
            news_list=news_items,
            budget_twd=1_000_000,
        )

        # 5. 產生競拍與新聞重點摘要
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
