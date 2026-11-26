# Waterson Inventory Lookup（庫存查詢）

輸入型號 → 美國能出幾組、台灣能組幾組、卡在哪。同一支引擎提供兩個入口：
- **網頁**（Streamlit Cloud）：https://inventory-lookup-tool-nfvszxrhdk23dmjgrj4sbd.streamlit.app/
- **JSON API**（Google Cloud Run，給 LINE bot / Grok Bot）：https://waterson-inventory-api-i3wpftru7q-uc.a.run.app

> **完整設計邏輯、業務規則、部署方式見 [`ARCHITECTURE.md`](ARCHITECTURE.md)。** 要改程式或重新架構前先讀那份。

## 查詢流程（摘要）
1. **HubSpot product**：讀 `wh_configuration` 把套組拆成單片（A3 → SA.SA.SA1），每片取 `wh_erp`（1CA 料號），組合件系列再讀 `specification` 拿 2CA 半成品。
2. **美國**（`us_inventory.py`，Sheet `1kxvd…`）：現成套組＋散片各倉庫存。美國可出 = max(現成套組, 散片湊組, 拆組)。
3. **台灣 AU1**（Drive 資料夾 `1smPY7…` 裡最新的 `庫存現量.xlsx`）：用料號算可組數（1CA → 不夠再用 2CA 補組），找出瓶頸。
4. 美國出不了貨時，列出其他有貨顏色；316 沒貨時，同色 304 排第一。

## 身分（由密碼決定，email 只做紀錄）
| | 內部（`STAFF_PASSWORD`） | 經銷商（`DEALER_PASSWORD`） |
|---|---|---|
| 語言 | 中英並列 | 純英文 |
| 版面 | 摘要表（美國現成｜美國可出｜台灣可組｜卡在）＋美國怎麼出＋台灣明細 | 大數字＋結論＋簡化表 |
| 台灣、倉別、料號 | 顯示 | 不顯示，只給數字（交期寫 1–2 週） |

外層還有 Streamlit Cloud 的 Google 登入，只有被邀請（Invite viewers）的 email 能進。

## 本機跑
```bash
cd app
pip install -r requirements.txt
export HUBSPOT_API_TOKEN="pat-na1-…"
export GOOGLE_SA_KEY="/path/to/shipping-quote-486901-…json"   # 美國表＋台灣資料夾共用
streamlit run app.py                       # 網頁
python3 inventory_core.py "K51M-450-C4-US19"   # 只跑引擎
uvicorn api:app --port 7860                # API（需 requirements-api.txt）
```

## 部署
- **網頁**：push 到 `servicehinge/inventory-lookup-tool` 的 `main` 會自動重部署。看不到新版時，用 GitHub `servicehinge` 登入 share.streamlit.io → app 的 ⋮ → **Reboot app**，再到網頁按 `Cmd + Shift + R`。Secrets 範本見 `.streamlit/secrets.toml.example`。
- **API**：把 `api.py`、`inventory_core.py`、`us_inventory.py` 複製到 `../api-hf-space/`，在那個資料夾執行 `gcloud run deploy`（完整指令見 `ARCHITECTURE.md` §7.2，**cd 和 deploy 要寫在同一行**）。

## ⚠️ 改程式前
`app.py`、`inventory_core.py` 的工作區混著還沒 commit 的「生管」功能。commit 要用外科手術式，只推這次的修改。流程見 `ARCHITECTURE.md` §7.4。

## 檔案
- `inventory_core.py` — 引擎：`lookup()`、`us_ship()`、`lookup_all_colors()`、`color_alternatives()`（含 CLI）。
- `us_inventory.py` — 美國表讀取。
- `app.py` — Streamlit 前端。
- `api.py` — FastAPI（`/lookup`、`/colors`、`/healthz`），排 LINE 用文字。
- `ARCHITECTURE.md` — 架構與設計邏輯。
