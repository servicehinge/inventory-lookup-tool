# Waterson 庫存查詢系統：架構與設計邏輯

> 給之後要維護或**重新架構**這套系統的人（或 AI）看。讀完這份就能知道：資料從哪來、怎麼算、畫面為什麼長這樣、哪些規則不能改。
> 最後更新：2026-11-26（commit `2503149`）。

---

## 1. 這套系統在解決什麼

業務常被客戶問「這個型號有沒有貨、多久能出」。以前常常查不到，主要原因是**拿套組型號去查，但庫存其實是記在拆開的單片上**。

這套系統輸入一個型號，回答三件事：
1. **美國現在能出幾組**（含「用較小套組＋散片湊」的出法）
2. **台灣能組幾組**（只需包裝即可出貨）
3. **卡在哪**（缺哪一片、哪個料最少）

查的顏色沒貨時，會自動建議其他有貨的顏色；316 沒貨時，優先建議同色 304。

### 使用者與入口
| 入口 | 給誰 | 技術 | 網址 / 位置 |
|---|---|---|---|
| 網頁 | 內部業務、生管、經銷商 | Streamlit（Streamlit Community Cloud） | https://inventory-lookup-tool-nfvszxrhdk23dmjgrj4sbd.streamlit.app/ |
| JSON API | LINE bot、Grok Bot 等程式 | FastAPI（Google Cloud Run） | https://waterson-inventory-api-i3wpftru7q-uc.a.run.app |
| LINE | 內部同仁手機即時查 | GAS webhook → 呼叫上面的 API | repo `order-review-line` 的 `line-webhook-gas/Code.gs` |

---

## 2. 整體架構

```
            ┌──────────────── 資料來源（全部唯讀） ────────────────┐
            │ HubSpot products   美國庫存 Google Sheet   台灣 AU1 Drive 資料夾 │
            │ (拆解/料號/半成品)  (成品套組＋散片，各倉)  (工廠每天丟最新 xlsx)  │
            └────────┬─────────────────┬──────────────────┬──────────┘
                     ▼                 ▼                  ▼
              ┌───────────────────────────────────────────────┐
              │ inventory_core.py  ← 唯一的計算引擎（單一來源）   │
              │   lookup() / lookup_all_colors() / us_ship() /  │
              │   color_alternatives()                          │
              └───────┬───────────────────────────┬───────────┘
                      ▼                           ▼
              app.py（Streamlit 網頁）      api.py（FastAPI，Cloud Run）
                                                  │  回 text（排好版）＋ data（JSON）
                                       ┌──────────┴──────────┐
                                  LINE bot（GAS）          Grok Bot
```

**設計原則：計算邏輯只有一份。** 網頁、LINE、API 都呼叫同一個 `inventory_core.py`，數字永遠一致。各入口只負責「怎麼呈現」，不自己算。

### 檔案
| 檔案 | 角色 |
|---|---|
| `inventory_core.py` | 引擎：讀三個來源、拆解、算可出/可組、替代建議。也可當 CLI 跑。 |
| `us_inventory.py` | 讀美國庫存 Google Sheet（分頁 `2026 ZOHO`、`2026 XXU加州`、`墊片庫存`）。 |
| `app.py` | Streamlit 網頁：登入、身分、單查、任何顏色、批次、替代建議。 |
| `api.py` | FastAPI：`/lookup`、`/colors`、`/healthz`；把結果排成 LINE 用的文字。 |
| `../api-hf-space/` | Cloud Run 部署包（`api.py`＋`inventory_core.py`＋`us_inventory.py`＋`Dockerfile`）。**不在 git 裡**，從 `app/` 複製過去。 |
| `../learning/` | 各系列的學習筆記與「新料號型態辨識 Playbook」。 |

---

## 3. 資料來源

### 3.1 HubSpot products（結構：怎麼拆、料號是什麼）
- 用 `/crm/v3/objects/products/search`，以**系列**（型號前兩段，如 `K51M-450`）為單位一次撈進記憶體（`HubSpotCatalog`）。
- 用到的欄位：

| 欄位 | 意義 | 例子 |
|---|---|---|
| `name` | 產品名（＝型號，引擎用它比對） | `K51M-450-C4-US19` |
| `hs_sku` | SKU（含角型與鋼級） | `K51M-450-C4-SQ-US19-304` |
| `wh_configuration` | 套組配置，用 `.` 分隔單片 | `DS.SA.SB.SA1` |
| `wh_erp` | 單片的 ERP 料號（1CA） | `1CAWAT4501B` |
| `specification` | 單片 → 2CA 半成品的拆法（只有組合件系列才有） | `2CADP50013124B \| 背板黑色x2` |

- **套組本身的 `wh_erp` 是空的**，一定要拆到單片才有料號。
- token 只有讀取權限，程式不會寫回 HubSpot。

### 3.2 美國庫存（Google Sheet `1kxvd-e9oafYHjRs9i_Qqvr6Liq9FdwNJag3WEWqZzzU`）
- 分頁 `2026 ZOHO`（多倉）、`2026 XXU加州`，以產品名比對；同時有**成品套組列**和**散裝單片列**（例如 `K51M-450-DS-US19`）。散片列不是每個 SKU 都有，只看有貨的。
- 倉別：MI WLOK（Michigan）、CA ZOHO、NV ZOHO、CA XXU、Amazon USA。
- 墊片（Metal Door Shim）走 `墊片庫存` 分頁，單位是「包」（每包 10 片）。

### 3.3 台灣 AU1（Drive 資料夾 `1smPY7HOXIa7Z0XgpgwTANqwN5AkKkVgE`）
- 工廠每天把最新的 `庫存現量.xlsx` 丟進這個資料夾，**檔案 ID 每天都會換**，所以程式是「列出資料夾 → 取 modifiedTime 最新的檔」，優先讀 xlsx，沒有才退讀 `AU1_live_*` Google Sheet。
- 欄位：第 4 欄料號、第 5 欄名稱、第 9 欄安全存量、第 10 欄庫存。
- 讀不到資料夾就直接報錯，**絕不默默用舊資料**（避免報錯庫存害超賣）。
- 讀取身分：service account `shipping-quote-bot@shipping-quote-486901.iam.gserviceaccount.com`（要被分享該資料夾與美國表）。

---

## 4. 引擎演算法（`inventory_core.lookup(model)`）

### 4.1 型號
```
K51M - 450 - C4 - US19 [-316]
系列   尺寸   套組碼  顏色    鋼級（沒寫＝304）
```
- `normalize_model`：大寫、空白轉連字號、Swing Clear 各種寫法收斂成 `K51LSWRH`。
- `family_of`：前兩段（`K51M-450`）＝ HubSpot 撈資料的單位。
- 套組碼：第三段（`C4`），字母＝功能，數字＝一組幾片。**配置一律讀 `wh_configuration`，不要從套組碼推**（例如 B2 在不同系列可能是 DS.HA 或 HS.SA）。
- 顏色：US32D（霧面）、US19（黑）、695（古銅）、US4（金）、US10。顏色對照 `COLOR_NAMES`。

### 4.2 拆解（BOM）
`wh_configuration = "DS.SA.SB.SA1"` → 每片計數 → 每片去 catalog 找 `hs_sku`、`wh_erp`、`specification`。

### 4.3 美國
1. **現成套組**：套組型號在各倉的數量 → `us.set_stock`、`us.set_total`。
2. **散片**：每個單片型號在各倉的數量 → `us.components`。
3. **散片湊組**：`us.loose_sets = min(每片散片總數 ÷ 每組需求)`，任一片缺就是 0。
4. **拆組（split-fill）**，只在現成套組 = 0 時算：從 catalog 找同尺寸、同色、同角型、同鋼級，且配置是目標「真子集」的**最大**現成套組（例如 C4 → C3），差集的單片用散片補。`sets = min(子集套組數, 各差集散片 ÷ 每組需求)`。不寫死任何系列名。
5. **美國實際可出 `us_ship(r)` = max(現成套組, 散片湊組, 拆組)**。三種方式會用到同一批散片，所以**取最大、不能相加**。回傳 `(組數, 方式)`，方式是 `ready` / `loose` / `split` / `None`。

### 4.4 台灣（依型態分支）
| 型態 | 判斷 | 算法 |
|---|---|---|
| 標準兩層（K51M 等） | 單片有 1CA 料號 | `sets_1ca = min(1CA 庫存 ÷ 每組需求)` |
| 三層組合件（K51P / K51PS / K51GWL） | 單片 `specification` 有 2CA 行 | 先算 1CA；再用 2CA 補組：**共用件（背板）按整套需求加總**（Σ 每片用量 × 該片在套組的數量），`sets_from_sub = min(2CA 庫存 ÷ 每組需求)`；總數 = `sets_1ca + sets_from_sub` |
| Swing Clear | 型號含 SWRH/SWLH | 料號殘缺，改解析工廠品名（`TWSwingClear`） |
| Swing Clear 316 | 型號尾 -316 | 台灣無料，直接 0 |
| 門檔配件 WP01/WP02 | 名稱含 `-WP` | 單件，ERP 直接掛在產品上，算「件」 |
| Metal Door Shim | `is_shim_query` | 專用分支，美國算包、台灣算片 ÷ 10 |

- **瓶頸**：讓可組數最小的那個料。1CA 路徑報單片代碼（`SA`）；2CA 路徑報 2CA 料號，每個 2CA 帶 `used_by`（哪片在用、每片用幾個、套組裡幾片），畫面才能說「SA 半成品」。
- `specification` 的用量解析（`parse_sub_parts`）：只取 `2CA料號 | 描述xN` 的行，`xN` 接受 `x2`、`x 2`、`X2`、`×2`；沒寫就是 1。

### 4.5 任何顏色與替代建議
- `lookup_all_colors(base)`：對同套組的每個「純顏色」變體（方角 SQ ＋ 304，排除 14R/58R 圓角與 316）各跑一次 `lookup`，每列帶 `us_ship`、方式、拆組明細、台灣可組、瓶頸。依「美國可出 → 台灣可組」排序。純記憶體計算，大約 0.03 秒。
- `color_alternatives(model)`：查的型號**美國出不了貨**（`us_ship == 0`）時才觸發，列出其他有貨（美國可出 > 0 或台灣可組 > 0）的顏色。**型號是 -316 時，同色 304 排第一**。

---

## 5. 業務規則（不能隨便改）

這些是使用者明確拍板、或踩過雷才訂下的規則：

1. **任一單片查不到庫存 → 整組「無法確認」**：單片沒有 1CA 料號、也沒有 2CA 路徑時，可組數回 `None`（顯示「無法確認」），**絕不能拿有貨的單片假裝整組能出**（會超賣）。
2. **共用件按整套需求扣**（背板 SA/SA1 都用）。各單片各自算會重複計算而超賣。
3. **美國優先**：美國沒有完整套組時，先看能不能在美國湊出整組，再推台灣。美國湊組和台灣可組**兩邊都給**，由業務決定。
4. **三種美國出法擇一、不相加**（共用散片）。
5. **316 沒貨就看同色 304**。
6. **要同時講清楚「完整套組本身有幾組」和「美國實際可出」**：例如 C4 現成 0、可出 11（C3 套組＋DS 散片）。不能讓人以為有 11 組現成的 C4。
7. **交期文案要分三種情境**：
   - 內部「台灣可組」＝只需包裝即可出貨（少量可隔天、量多約 3 天）
   - 經銷商對外＝保守寫 1–2 週
   - 配件「需自台灣調貨」＝1–2 週（跨國調運）
8. **內部畫面一律中英並列**（國外同事也在看）；經銷商畫面純英文。
9. **經銷商不露台灣、倉別、料號**，只給數字。
10. **不顯示「低於安全存量」**在 API／LINE 文字裡（使用者說業務不需要）。網頁台灣明細表仍保留安全存量欄。
11. **庫存新鮮度**：API 的庫存表快取 10 分鐘（`DB_TTL=600`）、HubSpot 結構 15 分鐘（`CATALOG_TTL=900`）；網頁快取 30 分鐘（`st.cache_resource(ttl=1800)`）。使用者已拍板維持。

---

## 6. 呈現設計

### 6.1 網頁（`app.py`）

**登入**：外層是 Streamlit Cloud 的 Google 登入（只允許被邀請的 email），進站後再輸入 email＋密碼。**身分由密碼決定**，email 只做紀錄：
| 密碼（Secrets） | 身分 | 看到什麼 |
|---|---|---|
| `STAFF_PASSWORD` | internal（業務） | 中英並列、完整三地、料號、卡在哪 |
| `DEALER_PASSWORD` | dealer（經銷商） | 純英文，只有數字 |
網址帶 `?pw=&model=&any=1` 可直接進入並查詢（截圖、分享用）。

**內部：單一顏色和任何顏色用同一套版面**（2026-11-26 使用者定案：喜歡表格、討厭重複資訊）。
```
K51M-450-C4-US19 · SKU K51M-450-C4-SQ-US19-304

| 美國現成 C4 | 美國可出               | 台灣可組 | 卡在             |
| 0           | 11（C3 套組＋DS 散片） | 27       | SA 單片（剩 27） |
（說明：美國可出／台灣可組的定義）

C4 完整套組美國 0 組，改用現成 C3 套組＋散片在美國湊成整組
| 美國怎麼出    | 每組需 | 倉庫                    |
| 現成 C3 套組  | 1      | MI W-Lock 1、NV ZOHO 10 |
| DS 散片       | 1      | NV ZOHO 11              |

▸ 台灣明細（收合）：成品單片表＋（有的話）半成品表
（美國出不了貨時）其他顏色 / 316→304 替代建議表
```
- 共用元件：`_view_from_lookup` → `_summary_cells`（摘要列）→ `_render_us_how`（美國怎麼出）→ `_render_tw_detail`（台灣明細）。
- 任何顏色：上面是每色一列的摘要表（同樣欄位＋顏色欄），下面每個顏色一個收合區塊，內容跟單查的「美國怎麼出＋台灣明細」完全一樣。
- 「美國現成」欄名跟著套組碼走（C4、A3…）。
- 經銷商：沿用 `render_headline`（大數字＋結論橫幅）與簡化英文表，沒有改。
- 批次查詢：貼一整段文字，自動抓最多 10 個型號，每個型號一個收合區塊（內容 = 單查）。

### 6.2 API／LINE 文字（`api.fmt_single`，版型 A「結論先行」）
```
K51M-450-C4-US19
= DS×1 + SA×1 + SB×1 + SA1×1

美國 US：11 組/sets（拆組/split-fill）
  現成 C4 套組 0 / no ready C4 set
  用現成 C3 套組＋DS 散片 / ready C3 + loose DS

台灣 TW：27 組/sets
  卡在/bottleneck：SA 單片/piece
  剩 27，每組需 1 / 27 left, 1 per set
```
- 每個地點一行組數，下面縮排只講原因（缺哪片、卡在哪個料、剩幾、每組需幾）。逐倉、逐半成品明細**不放進文字**，只留在 JSON `data`。
- 美國出不了貨時，最後加上：
```
⚠ 316 美國無貨，可改 304 / 316 not in US — 304 options:
  304 同色/same color：美國 US 11 組（拆組/split-fill）｜台灣 TW 27 組
  US32D（Satin Stainless）：美國 US 10 組（拆組/split-fill）｜台灣 TW 21 組
```
（最多 4 個；JSON 另有 `alternatives` 陣列）
- `/colors?model=K51M-450-C4`：每個顏色一行「美國 US N 組（方式）｜台灣 TW N 組」。

### 6.3 API 端點與授權
| 端點 | 回傳 |
|---|---|
| `GET /healthz` | `{ok:true}` |
| `GET /lookup?model=&key=` | `{ok, found, text, data, alternatives}` |
| `GET /colors?model=<基礎型號>&key=` | `{ok, found, text, data}` |
- `key` 要等於 `API_KEY`（LINE 用）或 `EXTRA_API_KEYS`（逗號分隔，給其他呼叫端各一把，例如 Grok Bot）。錯誤回 401。
- 停掉某個呼叫端＝從 `EXTRA_API_KEYS` 拿掉那把再部署，不影響 LINE。

---

## 7. 部署與維運

### 7.1 Streamlit 網頁
- repo：`servicehinge/inventory-lookup-tool`（GitHub），branch `main`，main file `app.py`。push 後 Streamlit Cloud 自動重部署。
- **後台登入：share.streamlit.io 用 GitHub `servicehinge`**（Continue with GitHub）。看不到新版時：app 列的 ⋮ → **Reboot app**，再到網頁按 `Cmd + Shift + R`。
- Secrets（Settings → Secrets，範本 `.streamlit/secrets.toml.example`）：`HUBSPOT_API_TOKEN`、`STAFF_PASSWORD`、`DEALER_PASSWORD`、`[gcp_service_account]`。
- 這個 workspace 無法把 app 設成真正公開，所以用「Invite viewers by email」加人。被邀請的是 Viewer，只能使用、不能改設定或程式。

### 7.2 Cloud Run API
- 專案 `operations-487008`，服務 `waterson-inventory-api`，region `us-central1`，`--allow-unauthenticated`（靠 `key` 擋），min-instances 0（閒置後第一次查約 6 秒冷啟動）。
- 環境變數（`api-hf-space/.env.deploy.yaml`，不進版控、不上傳）：`HUBSPOT_API_TOKEN`、`GCP_SERVICE_ACCOUNT_JSON`、`API_KEY`、`EXTRA_API_KEYS`。
- 部署（**一定要在同一行先 cd 到部署資料夾**，曾因在 `app/` 執行而用錯設定檔，導致 API 回 500）：
```bash
cd "<專案絕對路徑>/api-hf-space" && pwd && \
  cp ../app/api.py ../app/inventory_core.py ../app/us_inventory.py . && \
  ~/google-cloud-sdk/bin/gcloud run deploy waterson-inventory-api --source . \
  --region us-central1 --allow-unauthenticated --env-vars-file .env.deploy.yaml \
  --memory 512Mi --project operations-487008
```
- gcloud 登入過期時：`~/google-cloud-sdk/bin/gcloud auth login`（betty@watersonusa.com）。
- 調快取不用重部署：`gcloud run services update waterson-inventory-api --region us-central1 --update-env-vars DB_TTL=<秒> --project operations-487008`。

### 7.3 LINE
- GAS `doPost` 把 K51/W41 型號交給 `apiLookup_`，打 `<INV_API_URL>/lookup?model=&key=<INV_API_KEY>`，回傳 `text`。連不上 API 時退回 GAS 原生查表，LINE 不會沒回應。
- GAS 改了要 `clasp push` ＋ `clasp deploy --deploymentId <正式 id>`。

### 7.4 改程式的流程（重要）
- `app.py`、`inventory_core.py` 的工作區長期混著**還沒 commit 的「生管」功能**（`render_plan`、`explode_*`、`PLANNER_PASSWORD`、三角色）。正式版只有內部／經銷商兩種身分。
- 所以 commit 要用「外科手術式」：備份完整檔 → 從 `git show HEAD:` 取正式版 → 只套用這次的修改 → commit／push → 把備份還原回工作區。commit 前用 `grep -c "render_plan\|explode_"` 確認是 0。
- 改完 `api.py`、`inventory_core.py`、`us_inventory.py` 要同步複製到 `api-hf-space/` 並重新部署 Cloud Run；只改 `app.py` 則不用。

### 7.5 本機測試
```bash
cd app
export HUBSPOT_API_TOKEN=...   # 讀取用 token
export GOOGLE_SA_KEY=".../shipping-quote-486901-dbd435d38327.json"
python3 inventory_core.py "K51M-450-C4-US19"      # 引擎
streamlit run app.py                              # 網頁
```
網頁無頭測試：`streamlit.testing.v1.AppTest`，設 `query_params["pw"]`、`["model"]`、`["any"]` 後 `run()`，檢查 `at.exception` 和 `at.table`。
注意：`DEFAULT_SA_KEY` 還寫著舊路徑 `~/Desktop/03Project/…`，本機一定要用 `GOOGLE_SA_KEY` 指定。

---

## 8. 已知限制與待辦
- 美國拆組只做「一個較小的現成套組＋散片」，不做多個套組組合。
- 生管功能（多型號×套數 → 單片與小零件需求、台灣缺口）已寫好但未上線，等「小零件 → AU1 料號」對照表。
- 美國散片列不是每個 SKU 都有，沒有列就當 0。
- 台灣數字只跟得上工廠上傳的頻率（每天一次）。
- 經銷商頁面仍是舊版面（大數字＋橫幅），尚未改成表格版。

## 9. 重新架構檢查清單
如果要用別的技術重做，以下行為要保留，並用這些案例驗證（數字會隨庫存變動，看邏輯）：
| 案例 | 要看到的行為 |
|---|---|
| `K51M-450-A4-US32D` | 美國有現成套組 → 美國可出 = 現成數；台灣卡在 SA 單片 |
| `K51M-450-C4-US19` | 現成 C4 = 0，但可出 = C3 套組＋DS 散片拆組；單查和任何顏色數字一致 |
| `K51P-500-A3-US19` | 1CA = 0，靠 2CA 半成品；背板每組 6（SA 2×2＋SA1 2×1）；卡在 SA 半成品；美國缺 SA1；出現其他顏色建議 |
| `K51M-450-C4-US19-316` | 台灣「無法確認」（SB 無料號）；替代建議第一列是同色 304 |
| `K51PS-500-B3-US32D` | 三層型號，半成品瓶頸 |
| `K51LSWRH-545-A3-US32D` | Swing Clear 分支；建議其他顏色 |
| 經銷商密碼查任何型號 | 純英文，沒有倉別／料號／台灣字樣 |
