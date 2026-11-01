# 📈 TWSE 台股競價拍賣追蹤、公司基本面 & 新聞摘要系統

自動從 [臺灣證券交易所 (TWSE) 競價拍賣公告](https://www.twse.com.tw/zh/announcement/auction.html) 抓取**尚未結束競拍**的股票與可轉債資訊，並自動整合：
1. **🏢 公司介紹與財務基本面摘要**：所屬產業、主要業務與產品營收比重、最新單月與累計營收規模、毛利率、營業利益率、稅前淨利率、ROE、單季與近四季累計 EPS、每股淨值。
2. **📰 Google News 相關財經新聞摘要與連結**。
3. **📬 彈性多收件人自動寄信**：預設寄送至 `joechen1579@gmail.com`，並支援隨時在網頁或設定檔中新增/移除多個 Email 地址。

---

## 📬 如何新增或修改收件人 Email 地址？

系統提供 **3 種方式** 讓您彈性管理多位收件人（所有收件人都會同時收到每日競拍日報）：

1. **從前端網頁直接新增**：
   - 在儀表板上方「📬 每日自動寄送收件人清單」輸入框直接輸入新 Email（例如 `abc@gmail.com`），按下 **「➕ 新增信箱」** 即可。
2. **直接修改 [`config.json`](./config.json)（雲端與本機皆適用）**：
   - 打開專案根目錄的 `config.json`，在 `"recipients"` 清單中加入任意多個信箱：
     ```json
     {
       "recipients": [
         "joechen1579@gmail.com",
         "second_email@gmail.com",
         "third_email@company.com"
       ],
       "sender_email": "joechen1579@gmail.com",
       "only_stocks": false
     }
     ```
3. **在 GitHub Actions 手動觸發時臨時填入**：
   - 在 GitHub Actions 點擊 `Run workflow` 時，可在彈出視窗填入額外的 Email（以逗號分隔）。
