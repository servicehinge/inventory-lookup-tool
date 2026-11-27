# AGENTS.md — Waterson Inventory Lookup

Guide for any AI agent or developer working on this repo. Read this first, then
[`ARCHITECTURE.md`](ARCHITECTURE.md) for full design detail (written in Chinese).

## Purpose

Sales staff used to get "not found" when checking stock, because they searched by the
**set** model (e.g. `K51M-450-C4-US19`), while inventory is actually stored as the
**individual hinge pieces** the set is made of (DS + SA + SB + SA1).

This program takes one model number and answers, in seconds:

1. **How many sets can ship from the US right now**: from ready sets, from loose pieces, or by
   combining a smaller ready set with loose pieces ("split-fill").
2. **How many sets Taiwan can assemble**: from finished pieces (1CA part numbers), topped up
   with semi-finished parts (2CA part numbers).
3. **What limits Taiwan** (the bottleneck part).
4. **Alternatives** when the US can't ship that model: other in-stock colors, and for
   `-316` models, the same color in 304 first.

There are three entry points, and all three use the same engine (`inventory_core.py`):

| Entry | Who | Where |
|---|---|---|
| Web (Streamlit) | Internal staff, dealers | https://inventory-lookup-tool-nfvszxrhdk23dmjgrj4sbd.streamlit.app/ |
| JSON API (FastAPI on Cloud Run) | LINE bot, Grok Bot | https://waterson-inventory-api-i3wpftru7q-uc.a.run.app |
| LINE | Staff on mobile | GAS webhook in repo `order-review-line` → calls the API |

The program is **read-only**. It never writes to HubSpot, the US sheet, or the Taiwan Excel.

## Files

| File | Role |
|---|---|
| `inventory_core.py` | Engine. `lookup()`, `us_ship()`, `lookup_all_colors()`, `color_alternatives()`, HubSpot catalog, Taiwan reader. Has a CLI. |
| `us_inventory.py` | Reads the US Google Sheet. |
| `app.py` | Streamlit web front end (internal / dealer views). |
| `api.py` | FastAPI: `/lookup`, `/colors`, `/healthz`; formats LINE/Grok text. |
| `order_enrich.py`, `customer_bg.py` | Order email → notification text with stock and customer background. **CLI only, not connected to any live flow.** |
| `ARCHITECTURE.md` | Full design, business rules, deploy steps (Chinese). |
| `../api-hf-space/` | Cloud Run deploy folder (not in git). Copies of `api.py`, `inventory_core.py`, `us_inventory.py` plus `Dockerfile` and secrets. |
| `../learning/` | Per-series learning notes; `PLAYBOOK.md` for new part families. |

## Inputs

### 1. HubSpot products (structure: how a set breaks down)
- API: `POST /crm/v3/objects/products/search`, loaded once per series (e.g. `K51M-450*`).
- Properties used:

| Property | Meaning | Example |
|---|---|---|
| `name` | Model number (main match key) | `K51M-450-C4-US19` |
| `hs_sku` | SKU, includes corner type and steel grade | `K51M-450-C4-SQ-US19-304` |
| `wh_configuration` | Pieces in the set, dot-separated | `DS.SA.SB.SA1` |
| `wh_erp` | Taiwan 1CA part number of a single piece | `1CAWAT4501B` |
| `specification` | Piece → 2CA semi-finished parts, lines like `2CA… \| description xN` | `2CADP50013124B \| 背板黑色x2` |

- Sets have no `wh_erp`; only single pieces do.
- K51GG names have no size (`K51GG-SA-US32D`); the engine also indexes a clean SKU alias
  (`K51GG-600-SA-US32D`).

### 2. US inventory: Google Sheet `1kxvd-e9oafYHjRs9i_Qqvr6Liq9FdwNJag3WEWqZzzU`
- Tab **`2026 ZOHO`**: column A = warehouse (`MI WLOK`, `CA ZOHO`, `NV ZOHO`), column B =
  SKU (only on the first row of each block; may contain aliases separated by newline or `=`).
  Quantity = the column whose header (first 6 rows) contains `TOTAL` or `Please see here`.
- Tab **`2026 XXU加州`** (CA XXU): column A = SKU, quantity = last number in the row.
  *This tab currently does not exist in the sheet; the code skips it silently.*
- Tab **`墊片庫存`**: Metal Door Shim, counted in packs of 10; column 2 = CA, column 3 = XXU.
- The sheet has both **ready set rows** and **loose piece rows** (e.g. `K51M-450-DS-US19`).

### 3. Taiwan AU1 inventory: Google Drive folder `1smPY7HOXIa7Z0XgpgwTANqwN5AkKkVgE`
- The factory uploads a new `庫存現量.xlsx` daily. The file ID changes every day, so the code
  lists the folder and takes the **most recently modified `.xlsx`**. If there's no xlsx, it
  falls back to the newest Google Sheet in the folder.
- First worksheet only. Columns (1-based): **D = part number (料號)**, **E = name**,
  **I = safety stock (安全存量)**, **J = quantity on hand (庫存量)**. Rows whose D is empty or
  `料號` are skipped.
- If the folder can't be read, the program **raises an error**. It never silently uses old data.
- Swing Clear pieces are matched by parsing factory names like `K51SW-6*4.5*4.5"-RH-SA-霧面`.

### Credentials (environment variables / secrets)
| Name | Used by | Notes |
|---|---|---|
| `HUBSPOT_API_TOKEN` | all | Read-only private-app token |
| `GOOGLE_SA_KEY` | local run | Path to service account JSON (`shipping-quote-bot@shipping-quote-486901…`) |
| `GCP_SERVICE_ACCOUNT_JSON` | Cloud Run | Whole SA JSON as a string |
| `[gcp_service_account]` | Streamlit Cloud | SA in `secrets.toml` |
| `STAFF_PASSWORD`, `DEALER_PASSWORD` | web | Password decides the role (internal / dealer) |
| `API_KEY`, `EXTRA_API_KEYS` | API | `?key=` must match; `EXTRA_API_KEYS` is comma-separated (Grok Bot has its own) |
| `DB_TTL` (600), `CATALOG_TTL` (900) | API | Cache seconds |
| `TW_FOLDER_ID`, `TW_SHEET_ID`, `TW_CSV`, `US_SHEET_ID` | optional | Overrides; `TW_CSV` is for local tests |

Template: `.streamlit/secrets.toml.example`. Real secrets are git-ignored.

## Outputs

The program **creates no files**. It returns results on screen (web), as JSON (API), or
as JSON on stdout (CLI).

### `lookup(model)` result
| Field | Meaning |
|---|---|
| `found` | Model exists in HubSpot |
| `decomposition` | `[{code, count, sku, erp}]`: pieces per set |
| `us.set_stock` / `us.set_total` | Ready complete sets per warehouse / total |
| `us.components` | Loose pieces per warehouse |
| `us.loose_sets` | Sets buildable from loose pieces only |
| `us.decomposed` | Split-fill plan: `via` (smaller ready set), `extra_pieces`, `sets` |
| `tw.components` | Each piece: `erp`, `qty`, `safety`, `need` per set |
| `tw.sets_1ca` | Sets from finished pieces |
| `tw.sets_from_sub` | Extra sets from 2CA semi-finished parts |
| `tw.assemblable_sets` | Total Taiwan sets (`None` = cannot confirm) |
| `tw.sub_parts` | 2CA parts with `qty`, `per_set`, `used_by` |
| `tw.bottleneck` | Piece code or 2CA part number that limits Taiwan |
| `tw.unconfirmable`, `tw.missing_erp` | Pieces with no part number → total unknown |

`us_ship(r)` returns `(sets, how)`, where `how` is one of `ready`, `loose`, or `split`.

### API
- `GET /lookup?model=&key=` returns `{ok, found, text, data, alternatives}`.
  `text` is the ready-to-send message: conclusion first (US line, TW line, bottleneck,
  then alternatives).
- `GET /colors?model=<base>&key=` returns one line per color: US can ship | TW can assemble.
- `GET /healthz`.

### Web (internal view)
Summary table: **US ready (set code) | US can ship | TW can assemble | Bottleneck**. Below it:
how the US would ship (which warehouses), then Taiwan detail (collapsed). Dealer view:
English only, no Taiwan, no warehouse, no part numbers.

## How to run

- **Python 3.11** (Dockerfile uses `python:3.11-slim`; local dev 3.11.1).
- Packages: `requirements.txt` (web) and `requirements-api.txt` (API). Both already exist.

```bash
cd app
python3 -m pip install -r requirements.txt          # web + engine
python3 -m pip install -r requirements-api.txt      # API (fastapi, uvicorn)

export HUBSPOT_API_TOKEN="pat-na1-…"
export GOOGLE_SA_KEY="/Users/weichuchen/Desktop/01 Projects/W claude code/PC202602 ask for shipping quote/shipping-quote-486901-dbd435d38327.json"
# DEFAULT_SA_KEY in code still points to an old path; always set GOOGLE_SA_KEY locally.

python3 inventory_core.py --model "K51M-450-C4-US19"               # engine only, JSON out
python3 inventory_core.py --model "K51M-450-C4" --all-colors       # every color
streamlit run app.py                                               # web, http://localhost:8501
API_KEY=test uvicorn api:app --port 7860                           # API
curl "http://localhost:7860/lookup?model=K51M-450-C4-US19&key=test"
```

### Deploy
- **Web**: push to `main` of `servicehinge/inventory-lookup-tool`. Streamlit Cloud redeploys.
  If the old version still shows, sign in to share.streamlit.io with GitHub `servicehinge`,
  open the app menu and choose **Reboot app**, then hard-refresh with Cmd+Shift+R.
- **API**: copy `api.py`, `inventory_core.py`, `us_inventory.py` into `../api-hf-space/`, then
  run the `cd` and the deploy in **one command** (a separate `cd` may not persist, and
  deploying from `app/` uploads secrets):
  ```bash
  cd "<abs path>/api-hf-space" && pwd && ~/google-cloud-sdk/bin/gcloud run deploy waterson-inventory-api \
    --source . --region us-central1 --allow-unauthenticated --env-vars-file .env.deploy.yaml \
    --memory 512Mi --project operations-487008
  ```

## Business rules (from the code; do not change without the owner's approval)

1. **Break every set down using `wh_configuration`.** Never infer pieces from the set code
   (B2 is `HS.SA` in K51GG but different in other series).
2. **Any piece without a part number makes the whole set "cannot confirm"** (`None`), unless
   it has a 2CA route. Never report a number from the pieces that are in stock, because
   that oversells.
3. **Taiwan sets = finished-piece sets + semi-finished top-up.** 1CA sets =
   min(piece qty ÷ pieces per set). Remaining demand is filled from 2CA parts.
   **Shared 2CA parts** (e.g. a backplate used by both SA and SA1) are summed per whole set
   before dividing, so they're not double-counted.
4. **Bottleneck**: if any sets come from 2CA, or there's no 1CA at all, report the tightest
   2CA part (the 1CA stock is already used up). Otherwise report the tightest piece.
5. **US can ship = max(ready sets, loose-piece sets, split-fill sets).** The three methods
   share the same loose pieces, so **never add them together**.
6. **Split-fill**: only when ready complete sets are 0. Use the largest ready set of the same
   size, color, corner and steel grade whose pieces are a strict subset of the target.
   Fill the missing pieces from US loose stock.
7. **Always show the complete set's own count too.** For example: "ready C4: 0, can ship
   11 (C3 set + DS loose)".
8. **US first.** Show both the US and Taiwan numbers; sales decides.
9. **Alternatives** appear only when US can ship 0. They list other pure colors (square
   corner, 304) that have US or Taiwan stock. For `-316`, the same color in 304 comes first.
10. **Defaults**: no steel grade means 304; square corner (`SQ`) is the default. Round
    corners (`14R`/`58R`), `316`, `TBD` and `…orUS19` variants are excluded from "any color".
11. **Swing Clear**: every spelling becomes `K51LSWRH`; LH is looked up as RH (only RH is
    stocked).
12. **Door stops (WP) and Metal Door Shim** are single items. US uses the SKU, Taiwan uses the
    part number. Shims are in packs of 10, so Taiwan packs = pieces ÷ 10.
13. **US exact match only** counts as stock. Near matches are returned only as `candidates`.
14. **Lead-time wording**: internal Taiwan = packaging only (next day to ~3 days, then
    shipping to the US). Dealer = "approx. 1–2 weeks". Accessories from Taiwan = 1–2 weeks.
15. **Dealers never see** Taiwan, warehouse names or part numbers.
16. **API/LINE text does not show "below safety stock"** (owner decision). The web Taiwan
    table still has a safety column.
17. **Never use stale Taiwan data**: if the Drive folder can't be read, fail loudly.

## How to verify

1. **Regression cases.** Numbers change with stock, so check the *behavior*:

| Model | Expected behavior |
|---|---|
| `K51M-450-A4-US32D` | US has ready sets → US can ship = ready count |
| `K51M-450-C4-US19` | Ready C4 = 0 but can ship via C3 set + DS loose; single and any-color views agree |
| `K51P-500-A3-US19` | 1CA = 0, uses 2CA; backplate 6 per set; bottleneck "SA semi-finished"; other colors suggested |
| `K51M-450-C4-US32D-316` | 304 same-color suggestion listed first |
| `K51GG-600-SA-US32D` (also `K51GG-SA-US32D`, full SKU) | Found; TW = 8 finished + 11 semi-finished; bottleneck "SA semi-finished" |
| `K51GG-B3` | Sizeless set splits into DS/HS/SA; any-color table renders |
| `K51LSWRH-450-A3-US32D` | Swing Clear branch works |
| Dealer password, any model | English only; no Taiwan, warehouse or part numbers |

2. **Hand check one model end to end.** Take the HubSpot `wh_configuration` and each piece's
   `wh_erp`. Find those part numbers in today's `庫存現量.xlsx` (column J) and the model's rows
   in the US sheet. Recompute the numbers with the rules above.
3. **Web headless test.** Use `streamlit.testing.v1.AppTest`: set `query_params["pw"]`,
   `["model"]` and optionally `["any"]="1"`, call `run()`, then assert `at.exception` is empty
   and read `at.table`.
4. **API.** Call `/lookup` with the cases above and compare `text` with the web summary.
   They must agree.
5. Before committing, run `git status` and make sure no secrets are staged
   (`secrets.toml`, `*.json`, `.env.deploy.yaml`, `.api_key`, `.grok_api_key`).

## Do not

- **Never overwrite or modify the source data**: the Taiwan `庫存現量.xlsx` in Drive, the US
  Google Sheet, or HubSpot. The program only reads. If you ever add an export or report,
  **save it as a new file with the date in the name** (e.g.
  `inventory_lookup_2026-11-27.xlsx`), never on top of an original.
- Don't hard-code "the 3rd segment is the set code". Use `set_code_of` / `base_of` /
  `is_base_only`, because K51GG names have no size.
- Don't add US ship methods together, and don't report a number when a piece is missing a
  part number.
- Don't deploy Cloud Run from `app/`; it would upload `.streamlit/secrets.toml`.
- Don't commit or print secrets. Grok Bot and LINE use separate API keys; don't mix them up.
- Don't revive the removed "production planner" draft (`render_plan`, `explode_*`,
  `PLANNER_PASSWORD`). It was deleted on 2026-11-26 and will be re-planned from scratch.
