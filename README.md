# Waterson Inventory Lookup（庫存查詢網頁）

輸入型號 → 整合三地庫存。Streamlit 網頁，沿用運費工具的 Streamlit Cloud 套路。

## 查詢流程（引擎）
1. **HubSpot product**：讀 `wh_configuration` 把套組拆成單片（A3 → SA.SA.SA1），每片取 `hs_sku` + `wh_erp`（ERP 料號）。
2. **美國**（`us_inventory.py`，Sheet `1kxvd…`）：成品(套組型號) + 零散零件(單片型號) 各倉庫存（MI WLOK / CA ZOHO / CA XXU / Amazon）。
3. **台灣 AU1**（Sheet `1UrNy8…`）：用 ERP 料號比對工廠庫存，算可組裝組數 + 安全存量示警。

## 身分（用「密碼」判斷，不是 email）
進站輸 email + 密碼；**身分由密碼決定**（email 只記錄，無法提權）：
- `STAFF_PASSWORD` → 內部版　｜　`DEALER_PASSWORD` → 經銷商版

| | 內部（員工密碼） | 經銷商（經銷商密碼） |
|---|---|---|
| 語言 | 中英並列 | 純英文（美式） |
| 美國庫存 | 完整數字 | ✅ 完整數字（只列有貨倉 + 合計） |
| 台灣庫存 | 單顆料號 + 可組數 + 安全存量示警 | 只給「製程中可再做 N 組，1–2 週」數量；不提台灣/不提倉別來源 |
| 拆解/料號 | 顯示 | 不顯示 |

> 經銷商即使 email 打 @watersonusa.com，只要用經銷商密碼就只能看經銷商版 → 可安全部署成 Public。
> 密碼放 Secrets，隨時可改；員工密碼只在公司流通，經銷商密碼等開放時才給。

## 本機跑
```bash
cd app
pip install -r requirements.txt
export HUBSPOT_API_TOKEN="pat-na1-…"
export GOOGLE_SA_KEY="/path/to/shipping-quote-486901-…json"   # 美國表+台灣表共用
# 離線測試台灣可用快照：export TW_CSV="/tmp/waterson_full.csv"
streamlit run app.py
```

## 部署到 Streamlit Cloud
1. push 這個資料夾到 GitHub repo。
2. share.streamlit.io → New app → 指到 repo 的 `app/app.py`。
3. App → Settings → Secrets，貼上 `.streamlit/secrets.toml.example` 的內容（填真值）。

## ⚠️ 上線前待辦
- [x] 台灣 AU1 表（`1UrNy8…`）已分享給 service account（2026-06-23）。
- [x] GitHub repo 已建並 push：`servicehinge/inventory-lookup-tool`（2026-06-24）。
- [ ] **Streamlit Cloud 部署（Public，靠密碼保護）**：免費版私有 app 只有 1 個名額（已被別支佔走），所以部署成 **Public**——有密碼閘門所以安全。
      New app → repo `servicehinge/inventory-lookup-tool`、branch `main`、main file `app.py` → Advanced/Secrets 貼上本機 `.streamlit/secrets.toml`（**含 STAFF/DEALER 密碼**）→ Deploy。
      密碼設定在 Secrets 的 `STAFF_PASSWORD` / `DEALER_PASSWORD`（實際密碼不寫在此檔，私下保管）。
- [ ] （之後）接進現有庫存 LINE bot（PC20260610 ordernotice），共用 `inventory_core.lookup()`。

## 檔案
- `inventory_core.py` — 引擎：`lookup(model)` 回三地整合 dict（含 CLI）。
- `us_inventory.py`   — 美國表讀取（複製自 PC20260610，key 改吃環境變數）。
- `app.py`           — Streamlit 前端（email gate + 雙身分 + i18n）。
