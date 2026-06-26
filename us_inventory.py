#!/usr/bin/env python3
"""美國庫存讀取：讀「2026美國物流/庫存」表，模糊比對型號，回各倉現有庫存。
複製自 PC20260610TrelloOrderVerification/inventory_lookup.py，改成 key/creds 可由環境/呼叫端注入，
以便 Streamlit Cloud 部署（service account JSON 走 st.secrets）。

兩分頁結構不同：
  - 2026 ZOHO   ：多倉（A欄倉庫、B欄SKU 只在首列），現量 = 標題含 TOTAL/「Please see here」那一欄（動態定位）。
  - 2026 XXU加州：單一倉(CA XXU)，A欄=SKU，現量 = 該列最後一個數字（流水餘額）。
SKU 常含別名（如「K51P-A2-US32D (SA.SA1)* = K51P-500-A2-US32D」），全部別名都拿來比。
"""
import os
import re
import gspread
from google.oauth2.service_account import Credentials

SHEET_ID = os.environ.get("US_SHEET_ID", "1kxvd-e9oafYHjRs9i_Qqvr6Liq9FdwNJag3WEWqZzzU")
DEFAULT_KEY = "/Users/weichuchen/Desktop/03Project/PC202602 ask for shipping quote/shipping-quote-486901-dbd435d38327.json"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
TABS = ["2026 ZOHO", "2026 XXU加州"]


def norm(s):
    return re.sub(r'[^A-Z0-9]', '', (s or '').upper())


def core(n):
    """製造 SKU → 銷售 SKU：去掉變體標記 SQ 與結尾的 304（304=標準）。"""
    n = n.replace('SQ', '')
    if n.endswith('304'):
        n = n[:-3]
    return n


def finish_of(raw):
    m = re.search(r'US\d+[A-Z]?(?![A-Za-z0-9])', (raw or '').upper())
    return m.group(0) if m else ''


def is_num(c):
    try:
        float(str(c).replace(',', '').strip()); return True
    except Exception:
        return False


def to_int(c):
    try:
        return int(float(str(c).replace(',', '').strip()))
    except Exception:
        return 0


def sku_aliases(cell):
    out = []
    for part in re.split(r'[\n=＝]', cell or ''):
        p = part.strip().lstrip('(').strip()
        p = re.split(r'\s*[（(]', p)[0].strip()
        if p and 'K51' in p.upper():
            out.append(p)
    return out


def find_total_col(rows):
    for r in rows[:6]:
        for ci, c in enumerate(r):
            u = (c or '').upper()
            if 'TOTAL' in u or 'PLEASE SEE HERE' in u:
                return ci
    return None


def parse_tab(ws):
    rows = ws.get_all_values()
    if not rows:
        return []
    tcol = find_total_col(rows)
    header0 = (rows[0][0] if rows[0] else '').strip().upper()
    entries = []
    if tcol is not None and header0.startswith('WAREHOUSE'):
        cur, cur_raw = [], ''
        for r in rows[1:]:
            wh = (r[0] if len(r) > 0 else '').strip()
            item = (r[1] if len(r) > 1 else '').strip()
            if item:
                cur, cur_raw = sku_aliases(item), item
            if not cur:
                continue
            qty = (r[tcol] if len(r) > tcol else '').strip()
            entries.append((cur, cur_raw, wh, qty))
    else:
        wh = "CA XXU" if "XXU" in ws.title else ws.title
        for r in rows[1:]:
            item = (r[0] if len(r) > 0 else '').strip()
            if not item:
                continue
            nums = [c for c in r[1:] if is_num(c)]
            qty = nums[-1] if nums else ''
            entries.append((sku_aliases(item), item, wh, qty))
    return entries


def _classify(entries, model):
    target = norm(model)
    res = {"model": model, "matched": "", "inStock": False, "stock": [], "candidates": [], "note": ""}
    if not target:
        res["note"] = "無型號可查"
        return res
    ctarget = core(target)
    tfin = finish_of(model)
    exact, near = [], []
    for aliases, raw, wh, qty in entries:
        nlist = [norm(x) for x in aliases]
        if target in nlist or (ctarget and ctarget in nlist):
            exact.append((raw, wh, qty))
        elif any((target and (target in n or n.startswith(target[:max(8, len(target) - 3)]))) for n in nlist if n):
            if tfin and finish_of(raw) != tfin:
                continue
            near.append((raw, wh, qty))
    if exact:
        res["matched"] = model
        for raw, wh, qty in exact:
            res["stock"].append({"warehouse": wh, "sku": raw.split('\n')[0].strip(), "qty": qty})
        res["inStock"] = any(to_int(s["qty"]) > 0 for s in res["stock"])
        res["note"] = "精確命中"
    else:
        seen = set()
        for raw, wh, qty in near:
            key = (raw.split('\n')[0].strip(), wh)
            if key in seen:
                continue
            seen.add(key)
            res["candidates"].append({"sku": raw.split('\n')[0].strip(), "warehouse": wh, "qty": qty})
        res["note"] = "無精確命中；列最接近候選" if near else "兩分頁皆無相符/近似型號"
    return res


def _make_creds():
    """優先用環境變數指定的 service account JSON 路徑；Streamlit 端可改用 from_service_account_info。"""
    key = os.environ.get("GOOGLE_SA_KEY", DEFAULT_KEY)
    return Credentials.from_service_account_file(key, scopes=SCOPES)


class USInventoryDB:
    """一次認證、整表讀一次；之後 lookup() 全在記憶體比對。"""

    def __init__(self, gc=None):
        self.gc = gc or gspread.authorize(_make_creds())
        self.entries = []
        sh = self.gc.open_by_key(SHEET_ID)
        for t in TABS:
            try:
                ws = sh.worksheet(t)
            except Exception:
                continue
            self.entries.extend(parse_tab(ws))

    def lookup(self, model):
        return _classify(self.entries, model)
