#!/usr/bin/env python3
"""Inventory lookup JSON API — 包住 inventory_core，給 LINE bot（GAS）呼叫。

單一引擎：Streamlit 網頁與 LINE 共用同一份 inventory_core，邏輯永遠一致。
- GET /healthz                         健康檢查
- GET /lookup?model=...&key=...        查單一型號；回 {ok, found, text, data}
  text = 已排版好的 LINE 訊息（GAS 直接送出即可）；data = 結構化結果。
- GET /colors?model=...&key=...        任何顏色矩陣（base 型號）。

授權：所有查詢需帶 ?key=<API_KEY>（環境變數），擋外部濫用。
DB（US / TW / TW-swing-clear）與 HubSpot catalog 以 TTL 快取在記憶體，重複查秒回。
"""
import os
import time
import json
import tempfile

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse


def _boot_sa():
    """部署主機（HF Spaces / Cloud Run / Render）只給環境變數：把整段 service account JSON
    寫成暫存檔，設 GOOGLE_SA_KEY 給 gspread 用。"""
    if not os.environ.get("GOOGLE_SA_KEY") and os.environ.get("GCP_SERVICE_ACCOUNT_JSON"):
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        f.write(os.environ["GCP_SERVICE_ACCOUNT_JSON"])
        f.flush()
        os.environ["GOOGLE_SA_KEY"] = f.name


_boot_sa()

import inventory_core as ic
from us_inventory import USInventoryDB

API_KEY = os.environ.get("API_KEY", "")
DB_TTL = int(os.environ.get("DB_TTL", "600"))        # 庫存表快取秒數
CATALOG_TTL = int(os.environ.get("CATALOG_TTL", "900"))  # HubSpot catalog 快取秒數

app = FastAPI(title="Waterson Inventory API")

_dbs = {"t": 0, "us": None, "tw": None, "tw_sw": None}
_catalogs = {}  # family -> (timestamp, HubSpotCatalog)


def _get_dbs():
    now = time.time()
    if not _dbs["us"] or now - _dbs["t"] > DB_TTL:
        _dbs["us"] = USInventoryDB()
        _dbs["tw"] = ic.TWInventory()
        _dbs["tw_sw"] = ic.TWSwingClear()
        _dbs["t"] = now
    return _dbs["us"], _dbs["tw"], _dbs["tw_sw"]


def _get_catalog(family):
    now = time.time()
    hit = _catalogs.get(family)
    if hit and now - hit[0] < CATALOG_TTL:
        return hit[1]
    cat = ic.HubSpotCatalog(family)
    _catalogs[family] = (now, cat)
    return cat


def _check(key):
    if API_KEY and key != API_KEY:
        raise HTTPException(status_code=401, detail="bad key")


# ---------------- LINE 文字排版（單一來源，內部中英並列）----------------
def _us_loose_lines(r):
    """美國無現成套組時：列每個單片的美國散片庫存，並算「全用散片」可湊幾組、缺哪片。"""
    comps = {c["code"]: c for c in r["us"].get("components") or []}
    parts, caps = [], []
    for d in r["decomposition"]:
        c = comps.get(d["code"])
        total = c["total"] if c else 0
        wh = "、".join(f"{s['warehouse']} {s['qty']}" for s in (c["stock"] if c else []) if s["qty"] > 0)
        parts.append(f"{d['code']} {total}" + (f"（{wh}）" if wh else ""))
        caps.append((total // d["count"] if d["count"] else 0, d["code"]))
    if not parts:
        return []
    out = ["  散片/loose pieces（每組需/per set: "
           + ", ".join(f"{d['code']}×{d['count']}" for d in r["decomposition"]) + "）: " + ", ".join(parts)]
    sets = min(c for c, _ in caps)
    if sets > 0:
        out.append(f"  → 散片可湊/can build from loose: {sets} 組/sets")
    else:
        short = "、".join(code for c, code in caps if c == 0)
        out.append(f"  → 散片湊不出整組，缺/short: {short}")
    return out


def _tw_detail_lines(r):
    """台灣可組數的明細：成品單片、半成品逐項，並明講卡在哪個料（哪片單片用的）。"""
    tw = r["tw"]
    comps = tw.get("components") or []
    subs = tw.get("sub_parts") or []
    out = []
    if comps:
        caps = [(c["qty"] // c["need"] if c["need"] else 0) for c in comps]
        out.append("  成品單片/finished pieces: "
                   + ", ".join(f"{c['code']} {c['qty']}（每組需/per set {c['need']}）" for c in comps)
                   + f" → {min(caps)} 組/sets")
    if not subs:
        b = next((c for c in comps if c["code"] == tw.get("bottleneck")), None)
        if b:
            out.append(f"  卡在/Bottleneck: {b['code']} 單片/piece（庫存/stock {b['qty']}，每組需/per set {b['need']}）")
        return out

    out.append(f"  半成品補組/from sub-parts: {tw.get('sets_from_sub', 0)} 組/sets")
    rows = []
    for s in subs:
        cap = s["qty"] // s["per_set"] if s["per_set"] else 0
        users = "、".join(f"{u['code']} 每片{u['per_piece']}個×{u['pieces']}片" for u in s.get("used_by") or [])
        rows.append((cap, s, users))
    low = min(c for c, _, _ in rows)
    for cap, s, users in rows:
        mark = "  ← 瓶頸/bottleneck" if cap == low else ""
        use = f"，{users}" if users else ""
        out.append(f"    {s['desc'] or s['erp']} {s['erp']}: 庫存/stock {s['qty']}，"
                   f"每組需/per set {s['per_set']}{use} → {cap} 組/sets{mark}")
    for cap, s, _ in rows:
        if cap != low:
            continue
        who = "、".join(u["code"] for u in s.get("used_by") or [])
        out.append(f"  卡在/Bottleneck: {s['desc'] or s['erp']}"
                   + (f"（{who} 用 / used by {who}）" if who else "")
                   + f"，庫存/stock {s['qty']}，每組需/per set {s['per_set']}")
    return out


def fmt_single(r):
    m = r["model"]
    if not r["found"]:
        return f"{m}\n查無此型號 / not found"
    lines = [r["set"].get("name", m)]
    bom = ", ".join(f"{d['code']}×{d['count']}" for d in r["decomposition"])
    if bom:
        lines.append(f"拆解/BOM: {bom}")

    us = r["us"]
    lines.append("")
    if us["set_stock"] and us["set_total"] > 0:
        wh = ", ".join(f"{s['warehouse']} {s['qty']}" for s in us["set_stock"] if s["qty"] > 0)
        lines.append(f"美國/US: {wh}（合計/total {us['set_total']} sets）")
    else:
        lines.append("美國/US: 無現成套組 / no ready sets")
        lines.extend(_us_loose_lines(r))
        dec = us.get("decomposed")
        if dec:
            via = dec["via"]
            ex = "＋".join(e["code"] for e in dec["extra_pieces"])
            lines.append(f"  美國可拆組/US split-fill: {dec['sets']} sets"
                         f"（{via['code']}套組＋散片{ex} / ready {via['code']} + loose {ex}）")
            lines.append("  ※ 散片湊組與拆組共用同批散片，擇一、不可相加 / pick one, do not add up")

    tw = r["tw"]
    lines.append("")
    if tw.get("unconfirmable"):
        miss = "、".join(tw.get("missing_erp") or [])
        lines.append(f"台灣製程中/In process: 無法確認/unconfirmable（單片 {miss} 未建料號/missing ERP）")
    else:
        lines.append(f"台灣製程中/In process: {tw['assemblable_sets']} 組/sets")
        lines.extend(_tw_detail_lines(r))
    if tw.get("note"):
        lines.append(f"  ※ {tw['note']}")

    ua = us.get("alt_colors") or []
    ta = tw.get("alt_colors") or []
    if ua or ta:
        oos = us["set_total"] == 0 and (tw.get("unconfirmable") or tw["assemblable_sets"] == 0)
        lines.append("")
        lines.append("⚠ 此色無貨，其他可選顏色 / Out of stock — other finishes:"
                     if oos else "其他顏色 / Other finishes:")
        for a in ua:
            lines.append(f"  現貨/US {a['color_name']}({a['color']}): {a['total']} sets")
        for a in ta:
            lines.append(f"  製程/In-proc {a['color_name']}({a['color']}): {a['sets']} sets")
    return "\n".join(lines)


def fmt_grid(data):
    if not data["found"]:
        return f"{data['base']}\n查無變體 / no variants"
    lines = [f"{data['base']} — 各顏色 / by finish"]
    for r in data["rows"]:
        ip_disp = "無法確認/NA" if r.get("tw_unconfirmable") else r["in_process"]
        seg = f"{r['finish']} {r['color']}: 美國/US {r['us_total']}、製程/In-proc {ip_disp}"
        if r.get("tw_low"):
            seg += " ⚠"
        lines.append(seg)
    return "\n".join(lines)


# ---------------- routes ----------------
@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/lookup")
def lookup(model: str, key: str = "", role: str = "internal"):
    _check(key)
    model = ic.normalize_model(model)
    us, tw, tw_sw = _get_dbs()
    cat = _get_catalog(ic.family_of(model))
    r = ic.lookup(model, catalog=cat, us_db=us, tw_db=tw, tw_sw_db=tw_sw)
    return JSONResponse({"ok": True, "found": r["found"], "text": fmt_single(r), "data": r})


@app.get("/colors")
def colors(model: str, key: str = "", role: str = "internal"):
    _check(key)
    base = ic.normalize_model(model)
    us, tw, tw_sw = _get_dbs()
    cat = _get_catalog(ic.family_of(base))
    data = ic.lookup_all_colors(base, catalog=cat, us_db=us, tw_db=tw, tw_sw_db=tw_sw)
    return JSONResponse({"ok": True, "found": data["found"], "text": fmt_grid(data), "data": data})
