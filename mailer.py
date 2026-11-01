#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TWSE 競拍股票日報 Email 產生器與多收件人 Gmail SMTP 寄信模組
支援多位收件人設定 (預設包含 joechen1579@gmail.com)
"""

import html
import json
import os
import re
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List, Optional, Union


DEFAULT_RECIPIENT_EMAIL = "joechen1579@gmail.com"
BASE_DIR = Path(__file__).resolve().parent
CONFIG_JSON_PATH = BASE_DIR / "config.json"


def parse_email_list(raw: Union[str, List[str], None]) -> List[str]:
    """將字串（支援逗號、分號、空白或換行分隔）或陣列解析為去重後的 Email 清單"""
    if not raw:
        return []
    if isinstance(raw, list):
        items = []
        for entry in raw:
            items.extend(re.split(r"[,;\s\n]+", str(entry).strip()))
    else:
        items = re.split(r"[,;\s\n]+", str(raw).strip())

    seen = set()
    valid_emails = []
    for em in items:
        em = em.strip()
        if em and "@" in em and em.lower() not in seen:
            seen.add(em.lower())
            valid_emails.append(em)
    return valid_emails


def get_configured_recipients(override_recipients: Union[str, List[str], None] = None) -> List[str]:
    """
    依優先順序取得收件人清單：
    1. 呼叫端傳入之 override_recipients
    2. 專案根目錄 config.json 中的 recipients 陣列 + 環境變數 RECIPIENT_EMAIL
    3. 預設值 joechen1579@gmail.com
    """
    if override_recipients:
        parsed = parse_email_list(override_recipients)
        if parsed:
            return parsed

    combined: List[str] = []
    if CONFIG_JSON_PATH.exists():
        try:
            cfg = json.loads(CONFIG_JSON_PATH.read_text(encoding="utf-8"))
            combined.extend(parse_email_list(cfg.get("recipients", [])))
        except Exception:
            pass

    env_emails = os.environ.get("RECIPIENT_EMAIL", "")
    if env_emails:
        combined.extend(parse_email_list(env_emails))

    deduped = parse_email_list(combined)
    return deduped if deduped else [DEFAULT_RECIPIENT_EMAIL]


def build_email_subject(report_data: Dict[str, Any]) -> str:
    stats = report_data.get("stats", {})
    ref_date = report_data.get("reference_date", "")
    bidding = stats.get("bidding_count", 0)
    total = stats.get("total_active", 0)
    if bidding > 0:
        return f"📈【台股競拍日報】{ref_date} 進行中競拍共 {total} 檔（🔥 {bidding} 檔投標進行中）"
    return f"📊【台股競拍日報】{ref_date} 尚未結束競拍共 {total} 檔"


def render_email_html(
    report_data: Dict[str, Any],
    dashboard_url: str = "",
    recipients: Optional[List[str]] = None,
) -> str:
    """
    將競拍資料、公司介紹（行業、業務範圍、營收規模、利潤率）與新聞摘要渲染為 HTML Email。
    """
    updated_at = html.escape(report_data.get("updated_at", ""))
    ref_date = html.escape(report_data.get("reference_date", ""))
    source_url = html.escape(
        report_data.get("source_url", "https://www.twse.com.tw/zh/announcement/auction.html")
    )
    stats = report_data.get("stats", {})
    auctions = report_data.get("auctions", [])
    recipient_list = recipients or get_configured_recipients()
    recipients_display = html.escape(", ".join(recipient_list))

    badge_styles = {
        "bidding": ("#065f46", "#d1fae5", "🟢 投標進行中"),
        "upcoming": ("#1e40af", "#dbeafe", "🔵 即將開始投標"),
        "waiting_open": ("#92400e", "#fef3c7", "🟡 已截標・待開標"),
    }

    cards_html = []
    if not auctions:
        cards_html.append(
            """
            <div style="background:#ffffff;border:1px solid #e2e8f0;border-radius:12px;padding:28px;text-align:center;color:#475569;">
                <p style="font-size:16px;margin:0 0 8px 0;font-weight:600;">目前暫無尚未結束的競價拍賣標的</p>
                <p style="font-size:13px;margin:0;color:#64748b;">系統將持續每日自動追蹤臺灣證券交易所最新公告。</p>
            </div>
            """
        )
    else:
        for item in auctions:
            status_code = item.get("status_code", "bidding")
            fg_color, bg_color, badge_text = badge_styles.get(
                status_code, ("#334155", "#f1f5f9", item.get("status_label", ""))
            )
            cat_bg = "#ede9fe" if not item.get("is_cb") else "#f1f5f9"
            cat_fg = "#5b21b6" if not item.get("is_cb") else "#475569"

            sec_name = html.escape(item.get("security_name", ""))
            sec_code = html.escape(item.get("security_code", ""))
            market = html.escape(item.get("market", ""))
            issue_type = html.escape(item.get("issue_type", ""))
            underwriter = html.escape(item.get("underwriter", ""))
            min_price = html.escape(item.get("min_price", ""))
            qty = html.escape(item.get("auction_quantity", ""))
            min_qty = html.escape(item.get("min_bid_quantity", "1"))
            max_qty = html.escape(item.get("max_bid_quantity", ""))
            margin_pct = html.escape(item.get("margin_percentage", ""))
            fee = html.escape(item.get("processing_fee", ""))
            est_margin = f"{item.get('estimated_min_margin', 0):,}"
            bid_start = html.escape(item.get("bid_start_date", ""))
            bid_end = html.escape(item.get("bid_end_date", ""))
            open_date = html.escape(item.get("open_bid_date", ""))
            listing_date = html.escape(item.get("listing_date", ""))
            countdown = html.escape(item.get("countdown_text", ""))
            overview = html.escape(item.get("summary_overview", ""))
            news_digest = html.escape(item.get("summary_news_digest", ""))
            yahoo_url = html.escape(item.get("yahoo_url", "#"))
            goodinfo_url = html.escape(item.get("goodinfo_url", "#"))
            moneydj_url = html.escape(item.get("moneydj_url", "#"))

            # 公司基本面與財務指標
            cp = item.get("company_profile", {}) or {}
            cp_industry = html.escape(cp.get("industry") or "—")
            cp_business = html.escape(cp.get("revenue_mix") or cp.get("business_scope") or "—")
            cp_month_rev = html.escape(
                f"{cp.get('latest_month_revenue', '—')} (年增 {cp.get('latest_month_yoy', '—')})"
                if cp.get("latest_month_revenue")
                else "—"
            )
            cp_acc_rev = html.escape(
                f"{cp.get('accumulated_revenue', '—')} (年增 {cp.get('accumulated_yoy', '—')})"
                if cp.get("accumulated_revenue")
                else "—"
            )
            cp_gross = html.escape(cp.get("gross_margin") or "—")
            cp_op = html.escape(cp.get("operating_margin") or "—")
            cp_pretax = html.escape(cp.get("pretax_margin") or "—")
            cp_roe = html.escape(cp.get("roe") or "—")
            cp_eps = html.escape(cp.get("latest_quarter_eps") or "—")
            cp_4q_eps = html.escape(cp.get("recent_4q_eps") or "—")
            cp_bps = html.escape(f"{cp.get('bps')} 元" if cp.get("bps") else "—")
            cp_summary_text = html.escape(cp.get("summary_text") or "")

            news_items = item.get("news", [])
            news_rows = []
            if news_items:
                for n in news_items:
                    n_title = html.escape(n.get("title", ""))
                    n_url = html.escape(n.get("url", "#"))
                    n_source = html.escape(n.get("source", "新聞"))
                    n_date = html.escape(n.get("published_at", ""))
                    news_rows.append(
                        f"""
                        <li style="margin-bottom:8px;line-height:1.5;">
                            <a href="{n_url}" target="_blank" style="color:#2563eb;text-decoration:none;font-weight:500;">
                                {n_title}
                            </a>
                            <span style="font-size:12px;color:#64748b;margin-left:6px;">
                                — {n_source} ({n_date})
                            </span>
                        </li>
                        """
                    )
                news_list_html = f'<ul style="margin:8px 0 0 0;padding-left:20px;">{"".join(news_rows)}</ul>'
            else:
                news_list_html = '<p style="margin:6px 0 0 0;font-size:13px;color:#64748b;">暫無近期相關新聞</p>'

            cards_html.append(
                f"""
                <div style="background:#ffffff;border:1px solid #e2e8f0;border-radius:12px;padding:20px;margin-bottom:18px;box-shadow:0 1px 3px rgba(0,0,0,0.03);">
                    <div style="border-bottom:1px solid #f1f5f9;padding-bottom:12px;margin-bottom:14px;">
                        <span style="display:inline-block;background:{bg_color};color:{fg_color};font-size:12px;font-weight:700;padding:4px 10px;border-radius:999px;margin-right:6px;">
                            {badge_text}
                        </span>
                        <span style="display:inline-block;background:{cat_bg};color:{cat_fg};font-size:12px;font-weight:600;padding:4px 10px;border-radius:999px;margin-right:6px;">
                            {issue_type}
                        </span>
                        <span style="display:inline-block;background:#f0fdf4;color:#166534;font-size:12px;font-weight:600;padding:4px 10px;border-radius:999px;margin-right:6px;">
                            🏭 {cp_industry}
                        </span>
                        <span style="display:inline-block;font-size:12px;color:#dc2626;font-weight:600;">
                            ⏰ {countdown}
                        </span>
                        <h2 style="margin:10px 0 4px 0;font-size:20px;color:#0f172a;">
                            {sec_name} <span style="color:#475569;font-weight:500;font-size:17px;">({sec_code})</span>
                        </h2>
                        <div style="font-size:13px;color:#64748b;">
                            市場別：{market} ｜ 主辦券商：{underwriter} ｜ 
                            <a href="{yahoo_url}" target="_blank" style="color:#2563eb;text-decoration:none;">Yahoo股市</a> ・
                            <a href="{moneydj_url}" target="_blank" style="color:#2563eb;text-decoration:none;">MoneyDJ基本資料</a> ・
                            <a href="{goodinfo_url}" target="_blank" style="color:#2563eb;text-decoration:none;">Goodinfo</a>
                        </div>
                    </div>

                    <!-- 競拍規格表 -->
                    <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;margin-bottom:14px;font-size:13px;">
                        <tr>
                            <td style="padding:8px 10px;background:#f8fafc;border:1px solid #e2e8f0;width:22%;color:#64748b;">最低投標底價</td>
                            <td style="padding:8px 10px;background:#ffffff;border:1px solid #e2e8f0;width:28%;font-weight:700;color:#dc2626;font-size:15px;">{min_price} 元</td>
                            <td style="padding:8px 10px;background:#f8fafc;border:1px solid #e2e8f0;width:22%;color:#64748b;">競拍總數量</td>
                            <td style="padding:8px 10px;background:#ffffff;border:1px solid #e2e8f0;width:28%;font-weight:600;color:#0f172a;">{qty} 張 ({min_qty}~{max_qty}張)</td>
                        </tr>
                        <tr>
                            <td style="padding:8px 10px;background:#f8fafc;border:1px solid #e2e8f0;color:#64748b;">投標期間</td>
                            <td style="padding:8px 10px;background:#ffffff;border:1px solid #e2e8f0;font-weight:600;color:#0f172a;">{bid_start} ~ {bid_end}</td>
                            <td style="padding:8px 10px;background:#f8fafc;border:1px solid #e2e8f0;color:#64748b;">開標 / 撥券日</td>
                            <td style="padding:8px 10px;background:#ffffff;border:1px solid #e2e8f0;color:#0f172a;">{open_date} / {listing_date}</td>
                        </tr>
                        <tr>
                            <td style="padding:8px 10px;background:#f8fafc;border:1px solid #e2e8f0;color:#64748b;">保證金與門檻</td>
                            <td colspan="3" style="padding:8px 10px;background:#ffffff;border:1px solid #e2e8f0;color:#0f172a;">保證金成數 {margin_pct}%（每單最低預估約 <strong>NT$ {est_margin}</strong> 起，含處理費 {fee} 元）</td>
                        </tr>
                    </table>

                    <!-- 公司介紹與財務基本面摘要區塊 -->
                    <div style="background:#f0fdf4;border-left:4px solid #16a34a;padding:12px 14px;border-radius:6px;margin-bottom:12px;">
                        <div style="font-size:13px;font-weight:700;color:#14532d;margin-bottom:6px;">🏢 公司介紹、業務範圍、營收規模與利潤率摘要</div>
                        <p style="margin:0 0 8px 0;font-size:13px;color:#1e293b;line-height:1.6;">{cp_summary_text}</p>
                        <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;font-size:12px;background:#ffffff;border-radius:6px;">
                            <tr>
                                <td style="padding:6px 8px;border:1px solid #dcfce7;color:#475569;width:22%;"><strong>業務與產品比重</strong></td>
                                <td colspan="3" style="padding:6px 8px;border:1px solid #dcfce7;color:#0f172a;">{cp_business}</td>
                            </tr>
                            <tr>
                                <td style="padding:6px 8px;border:1px solid #dcfce7;color:#475569;width:22%;"><strong>最新單月營收</strong></td>
                                <td style="padding:6px 8px;border:1px solid #dcfce7;color:#0f172a;width:28%;">{cp_month_rev}</td>
                                <td style="padding:6px 8px;border:1px solid #dcfce7;color:#475569;width:22%;"><strong>當年度累計營收</strong></td>
                                <td style="padding:6px 8px;border:1px solid #dcfce7;color:#0f172a;width:28%;">{cp_acc_rev}</td>
                            </tr>
                            <tr>
                                <td style="padding:6px 8px;border:1px solid #dcfce7;color:#475569;"><strong>毛利率 / 營益率</strong></td>
                                <td style="padding:6px 8px;border:1px solid #dcfce7;color:#0f172a;font-weight:600;">{cp_gross} ／ {cp_op}</td>
                                <td style="padding:6px 8px;border:1px solid #dcfce7;color:#475569;"><strong>稅前淨利率 / ROE</strong></td>
                                <td style="padding:6px 8px;border:1px solid #dcfce7;color:#0f172a;">{cp_pretax} ／ {cp_roe}</td>
                            </tr>
                            <tr>
                                <td style="padding:6px 8px;border:1px solid #dcfce7;color:#475569;"><strong>單季 / 累計 EPS</strong></td>
                                <td style="padding:6px 8px;border:1px solid #dcfce7;color:#0f172a;font-weight:600;">{cp_eps} ／ {cp_4q_eps}</td>
                                <td style="padding:6px 8px;border:1px solid #dcfce7;color:#475569;"><strong>每股淨值</strong></td>
                                <td style="padding:6px 8px;border:1px solid #dcfce7;color:#0f172a;">{cp_bps}</td>
                            </tr>
                        </table>
                    </div>

                    <!-- 競拍與新聞重點摘要 -->
                    <div style="background:#f8fafc;border-left:4px solid #3b82f6;padding:12px 14px;border-radius:6px;margin-bottom:12px;">
                        <div style="font-size:13px;font-weight:700;color:#1e3a8a;margin-bottom:4px;">📌 競拍情報與新聞重點摘要</div>
                        <p style="margin:0 0 6px 0;font-size:13px;color:#334155;line-height:1.6;">{overview}</p>
                        <p style="margin:0;font-size:13px;color:#0f172a;line-height:1.6;">{news_digest}</p>
                    </div>

                    <div style="font-size:13px;">
                        <div style="font-weight:700;color:#334155;margin-bottom:4px;">📰 相關新聞連結（點擊閱讀全文）：</div>
                        {news_list_html}
                    </div>
                </div>
                """
            )

    dashboard_btn_html = ""
    if dashboard_url:
        safe_dash = html.escape(dashboard_url)
        dashboard_btn_html = f"""
        <a href="{safe_dash}" target="_blank" style="display:inline-block;background:#ffffff;color:#1e40af;font-weight:600;font-size:13px;padding:8px 16px;border-radius:8px;text-decoration:none;margin-right:8px;">
            🌐 開啟線上競拍儀表板
        </a>
        """

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TWSE 台股競價拍賣日報 ({ref_date})</title>
</head>
<body style="margin:0;padding:0;background-color:#f1f5f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;">
    <div style="max-width:720px;margin:0 auto;padding:24px 14px;">
        <!-- Header -->
        <div style="background:linear-gradient(135deg,#1e3a8a 0%,#2563eb 100%);color:#ffffff;border-radius:14px;padding:24px;margin-bottom:20px;">
            <div style="font-size:12px;opacity:0.85;letter-spacing:0.5px;margin-bottom:4px;">TWSE COMPETITIVE AUCTION DAILY DIGEST</div>
            <h1 style="margin:0 0 8px 0;font-size:24px;font-weight:800;">📈 台股最新競價拍賣公告、公司介紹 & 新聞摘要</h1>
            <p style="margin:0 0 16px 0;font-size:13px;opacity:0.9;">
                僅篩選「尚未結束競拍」之標的 ｜ 資料基準日：{ref_date} ｜ 更新時間：{updated_at}
            </p>
            <div>
                {dashboard_btn_html}
                <a href="{source_url}" target="_blank" style="display:inline-block;background:rgba(255,255,255,0.18);color:#ffffff;font-weight:600;font-size:13px;padding:8px 16px;border-radius:8px;text-decoration:none;border:1px solid rgba(255,255,255,0.35);">
                    🔗 證交所競拍公告原網頁
                </a>
            </div>
        </div>

        <!-- Summary KPI Row -->
        <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:20px;">
            <tr>
                <td width="32%" style="background:#ffffff;border:1px solid #e2e8f0;border-radius:10px;padding:12px 14px;text-align:center;">
                    <div style="font-size:12px;color:#64748b;">未結束競拍總數</div>
                    <div style="font-size:22px;font-weight:800;color:#0f172a;margin-top:2px;">{stats.get('total_active', 0)} <span style="font-size:12px;font-weight:500;">檔</span></div>
                </td>
                <td width="2%"></td>
                <td width="32%" style="background:#ecfdf5;border:1px solid #a7f3d0;border-radius:10px;padding:12px 14px;text-align:center;">
                    <div style="font-size:12px;color:#065f46;">🟢 投標進行中</div>
                    <div style="font-size:22px;font-weight:800;color:#059669;margin-top:2px;">{stats.get('bidding_count', 0)} <span style="font-size:12px;font-weight:500;">檔</span></div>
                </td>
                <td width="2%"></td>
                <td width="32%" style="background:#fffbeb;border:1px solid #fde68a;border-radius:10px;padding:12px 14px;text-align:center;">
                    <div style="font-size:12px;color:#92400e;">🟡 已截標・待開標</div>
                    <div style="font-size:22px;font-weight:800;color:#d97706;margin-top:2px;">{stats.get('waiting_open_count', 0)} <span style="font-size:12px;font-weight:500;">檔</span></div>
                </td>
            </tr>
        </table>

        <!-- Cards -->
        {"".join(cards_html)}

        <!-- Footer -->
        <div style="text-align:center;font-size:12px;color:#64748b;padding:16px 0;">
            本信件由「TWSE 競拍股票自動追蹤系統」自動整理發送至：{recipients_display}<br>
            資料來源：<a href="{source_url}" style="color:#475569;">臺灣證券交易所 (TWSE)</a>、MoneyDJ 財經百科 ＆ Google News
        </div>
    </div>
</body>
</html>
"""


def send_gmail_report(
    report_data: Dict[str, Any],
    sender_email: Optional[str] = None,
    app_password: Optional[str] = None,
    recipient_email: Union[str, List[str], None] = None,
    dashboard_url: str = "",
) -> Dict[str, Any]:
    """
    透過 Gmail SMTP 寄送 HTML 格式競拍日報（支援同時寄給多位收件人）。
    """
    sender = (sender_email or os.environ.get("GMAIL_USER") or DEFAULT_RECIPIENT_EMAIL).strip()
    password = (app_password or os.environ.get("GMAIL_APP_PASSWORD") or "").strip()
    password_clean = password.replace(" ", "")
    recipient_list = get_configured_recipients(recipient_email)

    if not password_clean:
        return {
            "ok": False,
            "error": (
                "尚未設定 Gmail 應用程式密碼 (GMAIL_APP_PASSWORD)。"
                "請在網頁設定面板或 GitHub Secrets 中填入 16 碼 Google 應用程式密碼。"
            ),
        }

    subject = build_email_subject(report_data)
    html_body = render_email_html(
        report_data, dashboard_url=dashboard_url, recipients=recipient_list
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"台股競拍自動助手 <{sender}>"
    msg["To"] = ", ".join(recipient_list)

    source_url = report_data.get(
        "source_url", "https://www.twse.com.tw/zh/announcement/auction.html"
    )
    plain_fallback = (
        f"台股競拍日報 ({report_data.get('reference_date', '')})\n"
        f"目前共有 {report_data.get('stats', {}).get('total_active', 0)} 檔尚未結束競拍標的。\n"
        f"請使用支援 HTML 的信箱檢視完整表格與新聞連結，或前往：{source_url}"
    )
    msg.attach(MIMEText(plain_fallback, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context, timeout=20) as server:
            server.login(sender, password_clean)
            server.sendmail(sender, recipient_list, msg.as_string())
        return {
            "ok": True,
            "message": f"已成功寄送競拍日報至 {len(recipient_list)} 個信箱：{', '.join(recipient_list)}！",
            "subject": subject,
            "recipients": recipient_list,
        }
    except Exception as e:
        return {
            "ok": False,
            "error": f"Gmail SMTP 寄送失敗: {e}",
        }
