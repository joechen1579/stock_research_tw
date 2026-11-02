#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TWSE 台股競價拍賣爬蟲、公司介紹摘要、新聞摘要、前端伺服器與每日自動寄信主程式

使用方式：
1. 啟動前端互動網頁伺服器（預設）：
   python3 main.py
   開啟瀏覽器前往 http://localhost:8000

2. 單次執行爬蟲 + 更新靜態資料 + 寄送 Email（適合 GitHub Actions 或排程）：
   python3 main.py --run-once --send-email
"""

import argparse
import datetime
import json
import mimetypes
import os
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, List

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from mailer import (
    CONFIG_JSON_PATH,
    DEFAULT_RECIPIENT_EMAIL,
    get_configured_recipients,
    parse_email_list,
    render_email_html,
    send_gmail_report,
)
from scraper import collect_active_auctions_with_news


BASE_DIR = Path(__file__).resolve().parent
DOCS_DIR = BASE_DIR / "docs"
DATA_JSON_PATH = DOCS_DIR / "data.json"
DOCS_CONFIG_JSON_PATH = DOCS_DIR / "config.json"
EMAIL_PREVIEW_PATH = DOCS_DIR / "email_preview.html"
ENV_FILE_PATH = BASE_DIR / ".env"


def load_dotenv_file() -> None:
    """讀取本機 .env 檔案至環境變數（不依賴第三方套件）"""
    if not ENV_FILE_PATH.exists():
        return
    try:
        for line in ENV_FILE_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and v:
                os.environ[k] = v
    except Exception as e:
        print(f"[WARN] 讀取 .env 失敗: {e}")


def load_config_json() -> Dict[str, Any]:
    default_cfg = {
        "recipients": get_configured_recipients(),
        "sender_email": os.environ.get("GMAIL_USER", DEFAULT_RECIPIENT_EMAIL),
        "only_stocks": True,
    }
    if CONFIG_JSON_PATH.exists():
        try:
            loaded = json.loads(CONFIG_JSON_PATH.read_text(encoding="utf-8"))
            loaded_recipients = parse_email_list(loaded.get("recipients", []))
            if loaded_recipients:
                default_cfg["recipients"] = loaded_recipients
            if loaded.get("sender_email"):
                default_cfg["sender_email"] = loaded["sender_email"]
            if "only_stocks" in loaded:
                default_cfg["only_stocks"] = bool(loaded["only_stocks"])
        except Exception:
            pass
    return default_cfg


def save_config_json(recipients: List[str], sender_email: str = "", only_stocks: bool = True) -> Dict[str, Any]:
    clean_recipients = parse_email_list(recipients) or [DEFAULT_RECIPIENT_EMAIL]
    cfg = load_config_json()
    cfg["recipients"] = clean_recipients
    if sender_email:
        cfg["sender_email"] = sender_email.strip()
    cfg["only_stocks"] = bool(only_stocks)

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(cfg, ensure_ascii=False, indent=2) + "\n"
    CONFIG_JSON_PATH.write_text(raw, encoding="utf-8")
    DOCS_CONFIG_JSON_PATH.write_text(raw, encoding="utf-8")
    return cfg


def save_dotenv_values(values: Dict[str, str]) -> None:
    """儲存設定至本機 .env 檔案"""
    existing: Dict[str, str] = {}
    if ENV_FILE_PATH.exists():
        for line in ENV_FILE_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                existing[k.strip()] = v.strip()

    for k, v in values.items():
        if v is not None and str(v).strip() != "":
            existing[k] = str(v).strip()
            os.environ[k] = str(v).strip()

    lines = [f"{k}={v}" for k, v in existing.items()]
    ENV_FILE_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _auction_unique_key(item: Dict[str, Any]) -> str:
    return f"{item.get('security_code', '')}_{item.get('bid_start_date', '')}_{item.get('open_bid_date', '')}"


def detect_auction_changes(
    old_auctions: List[Dict[str, Any]], new_auctions: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """
    比對上次儲存的競拍清單與最新爬取的競拍清單，找出：
    1. 新增的競拍標的 (new_items)
    2. 狀態發生變動的競拍標的 (status_changed_items)，例如「即將開始投標 -> 投標進行中」或「投標進行中 -> 已截標・待開標」
    """
    old_map: Dict[str, Dict[str, Any]] = {}
    for item in old_auctions:
        if item.get("is_cb"):
            continue
        old_map[_auction_unique_key(item)] = item

    new_items: List[Dict[str, Any]] = []
    status_changed_items: List[Dict[str, Any]] = []
    summary_list: List[str] = []

    for item in new_auctions:
        key = _auction_unique_key(item)
        name = item.get("security_name", "")
        code = item.get("security_code", "")
        new_status_code = item.get("status_code", "")
        new_status_label = item.get("status_label", "")

        if key not in old_map:
            item["change_badge"] = "🆕 新增競拍標的"
            new_items.append({"security_code": code, "security_name": name, "status_label": new_status_label})
            summary_list.append(f"🆕 新增競拍標的：{name} ({code}) — 目前狀態「{new_status_label}」")
        else:
            prev = old_map[key]
            old_status_code = prev.get("status_code", "")
            old_status_label = prev.get("status_label", "")
            if old_status_code != new_status_code:
                item["change_badge"] = f"🔔 狀態變動：{old_status_label} ➔ {new_status_label}"
                status_changed_items.append(
                    {
                        "security_code": code,
                        "security_name": name,
                        "old_status_code": old_status_code,
                        "old_status_label": old_status_label,
                        "new_status_code": new_status_code,
                        "new_status_label": new_status_label,
                    }
                )
                summary_list.append(
                    f"🔔 狀態變動：{name} ({code}) 由「{old_status_label}」轉為「{new_status_label}」"
                )

    has_changes = bool(new_items or status_changed_items)
    return {
        "has_changes": has_changes,
        "new_items": new_items,
        "status_changed_items": status_changed_items,
        "summary_list": summary_list,
    }


def update_all_artifacts(only_stocks: bool = True) -> Dict[str, Any]:
    """
    執行爬蟲、抓取公司基本面與財務數據、搜尋新聞、整理摘要，並輸出至 docs/data.json 與 docs/email_preview.html
    """
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip() or None

    # 先讀取舊的 docs/data.json 以供比對「新增標的」或「狀態變動」
    previous_auctions: List[Dict[str, Any]] = []
    if DATA_JSON_PATH.exists():
        try:
            old_data = json.loads(DATA_JSON_PATH.read_text(encoding="utf-8"))
            previous_auctions = old_data.get("auctions", []) or []
        except Exception:
            previous_auctions = []

    # 同步 config.json 至 docs/config.json 供前端讀取
    cfg = load_config_json()
    DOCS_CONFIG_JSON_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print("🔍 正在從臺灣證券交易所 (TWSE) 抓取最新「尚未結束競拍股票（不含可轉債）」公告、公司基本面與 Google 新聞...")
    report_data = collect_active_auctions_with_news(
        gemini_api_key=gemini_key,
        max_news_per_stock=5,
        only_stocks=only_stocks,
    )
    report_data["recipients"] = cfg["recipients"]

    # 比對是否有新增標的或狀態變更
    changes = detect_auction_changes(previous_auctions, report_data.get("auctions", []))
    report_data["changes"] = changes

    DATA_JSON_PATH.write_text(
        json.dumps(report_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    dashboard_url = os.environ.get("DASHBOARD_URL", "").strip()
    email_html = render_email_html(
        report_data, dashboard_url=dashboard_url, recipients=cfg["recipients"]
    )
    EMAIL_PREVIEW_PATH.write_text(email_html, encoding="utf-8")

    print(
        f"✅ 資料更新完成！共整理 {report_data['stats']['total_active']} 檔尚未結束競拍股票 "
        f"(已寫入 {DATA_JSON_PATH.name} 與 {EMAIL_PREVIEW_PATH.name})"
    )
    if changes["has_changes"]:
        print("🔔 偵測到競拍異動：")
        for line in changes["summary_list"]:
            print(f"   - {line}")
    else:
        print("ℹ️ 本次檢查無新增競拍標的，且既有標的狀態皆無變動。")
    return report_data


def load_cached_or_fetch() -> Dict[str, Any]:
    if DATA_JSON_PATH.exists():
        try:
            data = json.loads(DATA_JSON_PATH.read_text(encoding="utf-8"))
            data["recipients"] = get_configured_recipients()
            return data
        except Exception:
            pass
    return update_all_artifacts()


class AuctionDashboardHandler(BaseHTTPRequestHandler):
    """提供本機互動前端與 REST API 的 HTTP 伺服器"""

    def _send_json(self, payload: Dict[str, Any], status: int = 200) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _send_html(self, html_str: str, status: int = 200) -> None:
        raw = html_str.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]

        if path == "/api/status":
            recipients = get_configured_recipients()
            self._send_json(
                {
                    "ok": True,
                    "mode": "local_server",
                    "recipients": recipients,
                    "recipient_email": ", ".join(recipients),
                    "sender_email": os.environ.get("GMAIL_USER", DEFAULT_RECIPIENT_EMAIL),
                    "has_app_password": bool(os.environ.get("GMAIL_APP_PASSWORD")),
                    "has_gemini_key": bool(os.environ.get("GEMINI_API_KEY")),
                    "sinotrade_id": os.environ.get("SINOTRADE_ID", ""),
                    "has_sinotrade_password": bool(os.environ.get("SINOTRADE_PASSWORD")),
                }
            )
            return

        if path == "/api/data":
            data = load_cached_or_fetch()
            self._send_json(data)
            return

        if path == "/api/email-preview":
            data = load_cached_or_fetch()
            html_str = render_email_html(data, recipients=get_configured_recipients())
            self._send_html(html_str)
            return

        if path in ("/", "/index.html"):
            target_file = DOCS_DIR / "index.html"
        else:
            rel_path = path.lstrip("/")
            target_file = (DOCS_DIR / rel_path).resolve()
            if not str(target_file).startswith(str(DOCS_DIR)):
                self.send_error(403, "Forbidden")
                return

        if not target_file.exists() or not target_file.is_file():
            self.send_error(404, "File Not Found")
            return

        mime_type, _ = mimetypes.guess_type(str(target_file))
        content = target_file.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", f"{mime_type or 'text/plain'}; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        content_len = int(self.headers.get("Content-Length", 0))
        body_raw = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else "{}"
        try:
            body = json.loads(body_raw) if body_raw else {}
        except Exception:
            body = {}

        if path == "/api/refresh":
            try:
                data = update_all_artifacts()
                self._send_json({"ok": True, "data": data})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, status=500)
            return

        if path == "/api/config":
            try:
                raw_recipients = body.get("recipients") or body.get("recipient_email") or [DEFAULT_RECIPIENT_EMAIL]
                recipients_list = parse_email_list(raw_recipients) or [DEFAULT_RECIPIENT_EMAIL]
                sender_email = body.get("sender_email", DEFAULT_RECIPIENT_EMAIL)

                save_config_json(recipients=recipients_list, sender_email=sender_email)
                save_dotenv_values(
                    {
                        "RECIPIENT_EMAIL": ",".join(recipients_list),
                        "GMAIL_USER": sender_email,
                        "GMAIL_APP_PASSWORD": body.get("app_password", ""),
                        "GEMINI_API_KEY": body.get("gemini_api_key", ""),
                        "SINOTRADE_ID": body.get("sinotrade_id", ""),
                        "SINOTRADE_PASSWORD": body.get("sinotrade_password", ""),
                        "SINOTRADE_COOKIE": body.get("sinotrade_cookie", ""),
                    }
                )
                # 同步重新產生 email_preview.html
                if DATA_JSON_PATH.exists():
                    data = load_cached_or_fetch()
                    EMAIL_PREVIEW_PATH.write_text(
                        render_email_html(data, recipients=recipients_list), encoding="utf-8"
                    )
                self._send_json({"ok": True, "recipients": recipients_list})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, status=500)
            return

        if path == "/api/send-email":
            try:
                data = load_cached_or_fetch()
                raw_recipients = body.get("recipients") or body.get("recipient_email")
                res = send_gmail_report(
                    report_data=data,
                    sender_email=body.get("sender_email") or os.environ.get("GMAIL_USER"),
                    app_password=body.get("app_password") or os.environ.get("GMAIL_APP_PASSWORD"),
                    recipient_email=raw_recipients,
                )
                self._send_json(res, status=200 if res.get("ok") else 400)
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, status=500)
            return

        self.send_error(404, "API Endpoint Not Found")

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[HTTP] {self.address_string()} - {format % args}")


def start_daily_background_scheduler(daily_time_str: str = "08:30") -> None:
    """
    本機背景定時器：若電腦開著伺服器，每天於指定台灣時間 (預設 08:30) 自動更新；
    僅在有「新增競拍標的」或「狀態變動（如：即將開始 -> 投標進行中）」時才自動寄信。
    """
    def _worker() -> None:
        last_run_date = None
        tw_tz = datetime.timezone(datetime.timedelta(hours=8))
        while True:
            now_tw = datetime.datetime.now(tw_tz)
            hhmm = now_tw.strftime("%H:%M")
            if hhmm == daily_time_str and last_run_date != now_tw.date():
                last_run_date = now_tw.date()
                print(f"⏰ [每日定時任務] {now_tw.isoformat()} 開始自動檢查競拍股票異動...")
                try:
                    data = update_all_artifacts(only_stocks=True)
                    changes = data.get("changes", {}) or {}
                    if not changes.get("has_changes"):
                        print("ℹ️ [每日定時任務] 今日無新標的或狀態變動，略過寄信。")
                    elif os.environ.get("GMAIL_APP_PASSWORD"):
                        res = send_gmail_report(data)
                        print(f"✉️ [每日定時寄信結果] {res}")
                except Exception as e:
                    print(f"[ERROR] 每日定時任務失敗: {e}")
            time.sleep(30)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()


def main() -> None:
    load_dotenv_file()

    parser = argparse.ArgumentParser(description="TWSE 競拍股票自動爬蟲、公司基本面、新聞摘要與日報系統")
    parser.add_argument(
        "--run-once",
        action="store_true",
        help="僅執行一次爬蟲並產生 docs/data.json 與 docs/email_preview.html（不啟動網頁伺服器）",
    )
    parser.add_argument(
        "--send-email",
        action="store_true",
        help="執行完爬蟲後立即透過 Gmail SMTP 寄送日報至所有設定的收件人信箱（強制寄信）",
    )
    parser.add_argument(
        "--send-if-changed",
        action="store_true",
        help="僅在偵測到「新增競拍標的」或「狀態變動（如變為可投標）」時才寄送 Email",
    )
    parser.add_argument(
        "--only-stocks",
        action="store_true",
        default=True,
        help="僅保留股票競拍（預設啟用，排除轉換公司債 CB）",
    )
    parser.add_argument(
        "--include-cb",
        action="store_true",
        help="若指定此參數才包含轉換公司債 (預設僅抓取股票，不抓可轉債)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="本機網頁伺服器連接埠 (預設: 8000)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="啟動伺服器時不自動開啟瀏覽器視窗",
    )
    args = parser.parse_args()
    only_stocks = not args.include_cb

    try:
        report_data = update_all_artifacts(only_stocks=only_stocks)
    except Exception as e:
        if not args.run_once and DATA_JSON_PATH.exists():
            print(f"[WARN] 啟動時連線更新失敗 ({e})，先載入現有快取資料 docs/data.json")
            report_data = json.loads(DATA_JSON_PATH.read_text(encoding="utf-8"))
        else:
            raise

    changes = report_data.get("changes", {}) or {}
    should_send = False
    if args.send_email:
        should_send = True
    elif args.send_if_changed:
        if changes.get("has_changes"):
            should_send = True
        else:
            print("ℹ️ 本次排程檢查無新增標的或競拍狀態變動，因此不寄送 Email。")

    if should_send:
        dashboard_url = os.environ.get("DASHBOARD_URL", "").strip()
        result = send_gmail_report(report_data, dashboard_url=dashboard_url)
        if result.get("ok"):
            print(f"🎉 {result['message']}")
        else:
            print(f"⚠️ {result['error']}")
            if args.run_once:
                raise SystemExit(1)

    if args.run_once:
        return

    start_daily_background_scheduler(daily_time_str="08:30")
    server_addr = ("0.0.0.0", args.port)
    httpd = HTTPServer(server_addr, AuctionDashboardHandler)
    url = f"http://localhost:{args.port}"
    recipients = get_configured_recipients()
    print("\n" + "=" * 66)
    print(f"🚀 TWSE 競價拍賣前端儀表板已啟動：{url}")
    print(f"📧 目前設定收件人 ({len(recipients)} 位)：{', '.join(recipients)}")
    print("⏰ 已啟用每日 08:30 自動爬蟲與寄信排程（亦可部署至 GitHub Actions 免費雲端執行）")
    print("=" * 66 + "\n")

    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 伺服器已停止。")


if __name__ == "__main__":
    main()
