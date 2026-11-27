# 09 — Inventory Lookup: what it does and how to use it

*For non-technical readers. Developers: see `AGENTS.md`.*

## What problem does this solve?

Our hinges are sold as **sets**. For example, `K51M-450-C4-US19` is one set of four hinges:
DS + SA + SB + SA1. But our warehouses track stock by the **individual hinges** (pieces), and
Taiwan tracks it by **part numbers**. So searching for a set model often returned "not found",
even when we could actually ship it.

This tool does the matching for you. You type one model number and it tells you:

- **US ready**: how many complete sets are sitting in US warehouses right now.
- **US can ship**: how many sets the US can actually send, including sets we can put
  together from smaller sets and loose pieces.
- **TW can assemble**: how many sets Taiwan can make. These only need packing (next day for
  small quantities, about 3 days for larger ones), plus shipping to the US.
- **Bottleneck**: the part that stops Taiwan from making more. Restock this to ship more.
- **Alternatives**: if the US can't ship your color, other colors that are in stock. If the
  customer asked for 316 (marine grade) and it's out, the same color in 304 is listed first.

## Where does the data come from?

The tool reads three sources. It **only reads** them and never changes anything.

| Source | What it tells us |
|---|---|
| **HubSpot products** | Which pieces make up each set, and each piece's Taiwan part number |
| **US inventory Google Sheet** ("2026 ZOHO" tab only; shims from "墊片庫存") | Stock in MI W-Lock, CA ZOHO, NV ZOHO: complete sets and loose pieces. Amazon stock is **not** counted (check it by hand if needed); no other tab is read |
| **Taiwan daily stock file** (`庫存現量.xlsx`, uploaded by the factory to a Google Drive folder every day) | Stock of every Taiwan part number. The tool always uses the newest file |

If the Taiwan file can't be read, the tool shows an error instead of using old numbers.

## The rules it follows

1. **A set is split into its pieces** using the HubSpot product setup, never guessed from the
   model name.
2. **If any piece has no part number, the answer is "Cannot confirm".** The tool will not
   guess a number, because guessing could make us promise stock we don't have. Tell Betty
   so the part number can be added.
3. **US can ship** uses the *best* of three methods: ready sets, loose pieces only, or a
   smaller ready set plus loose pieces (e.g. a C3 set + one DS = a C4 set). These methods use
   the same loose pieces, so the numbers are **never added together**.
4. The tool always shows how many **complete sets** exist as well, so nobody thinks there
   are 11 ready C4 sets when the 11 are actually built from C3 + DS.
5. **Taiwan** counts finished pieces first, then makes up extra sets from semi-finished parts.
   A part shared by several pieces (such as a backplate) is counted once for the whole set.
   Safety stock is **not** subtracted, and sets made from semi-finished parts have the same
   lead time (next day to ~3 days).
6. **US first**: both the US and Taiwan numbers are shown, and sales decides.
7. **No-color searches** (e.g. `K51M-450-C4`) list every standard color (square corner, 304),
   with the color that can ship the most first. For **316**, search `K51M-450-C4-316` (or tick
   Any color on a 316 model): it lists 316 colors first, then 304 colors.
8. **Dealers** see English only, with no Taiwan, warehouse names, or part numbers. Their
   lead time is shown as "approx. 1–2 weeks".

## Where is the program?

| What | Where |
|---|---|
| Program files | Folder `PC20260623InventorySKULookup/app/` on Betty's Mac, also on GitHub (`servicehinge/inventory-lookup-tool`) |
| Website | https://inventory-lookup-tool-nfvszxrhdk23dmjgrj4sbd.streamlit.app/ |
| API for LINE and Grok Bot | https://waterson-inventory-api-i3wpftru7q-uc.a.run.app (runs on Google Cloud) |
| Staff how-to (Chinese) | `庫存查詢_使用說明.md` in the project folder |

## How to use it

**On the website**
1. Open the website and sign in with your @watersonusa.com Google account.
2. Enter your email and the **staff password** (ask Betty).
3. Type a model:
   - Full model with color (e.g. `K51M-450-C4-US19`) → that color only.
   - Model without color (e.g. `K51M-450-C4`), or tick **Any color** → all colors.
   - Paste a whole order into **Batch lookup** to check up to 10 models at once.

**On LINE**: send the model number to the company LINE bot.

**Grok Bot**: it calls the same API with its own key, so the numbers match the website.

Nothing needs to be "run" by hand. The website and API are always on, and they refresh
stock automatically (API every 10 minutes, website every 30 minutes).

## Who to contact

Wrong numbers, "Cannot confirm", a model not found, or access requests → **Betty**.
