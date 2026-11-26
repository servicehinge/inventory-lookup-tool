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


# 額外金鑰（逗號分隔），給 LINE 以外的呼叫端各發一把（如 Grok Bot），要停權只撤那一把、不影響 LINE。
EXTRA_API_KEYS = {k.strip() for k in os.environ.get("EXTRA_API_KEYS", "").split(",") if k.strip()}


def _check(key):
    if API_KEY and key != API_KEY and key not in EXTRA_API_KEYS:
        raise HTTPException(status_code=401, detail="bad key")


# ---------------- LINE 文字排版（單一來源，內部中英並列）----------------
# 版型 A「結論先行」（2026-11-26 使用者選定）：每個地點一行數字，下一行縮排只講原因（缺哪片 / 卡在哪個料）。
# 業務要的是「幾組＋卡在哪」，明細（逐倉、逐半成品、安全存量）不放進文字。


def _us_reason(r):
    """美國無現成套組時：標題給「實際可出」組數＋方式，下一行講怎麼湊；湊不出就講缺哪片。"""
    ship, how = ic.us_ship(r)
    us = r["us"]
    if how is None:
        comps = {c["code"]: c["total"] for c in us.get("components") or []}
        short = "、".join(d["code"] for d in r["decomposition"] if comps.get(d["code"], 0) < d["count"])
        have = "、".join(f"{d['code']} 散片 {comps[d['code']]}" for d in r["decomposition"] if comps.get(d["code"], 0) > 0)
        return ["美國 US：0 組/sets"] + ([f"  缺/short {short}" + (f"（{have}）" if have else "")] if short else [])
    out = [f"美國 US：{ship} 組/sets（{_HOW[how]}）"]
    if how == "split":
        dec = us["decomposed"]
        ex = "＋".join(e["code"] for e in dec["extra_pieces"])
        out.append(f"  用現成 {dec['via']['code']} 套組＋{ex} 散片 / ready {dec['via']['code']} + loose {ex}")
    else:
        out.append("  全用散片湊 / all from loose pieces")
    return out


def _tw_reason(r):
    """台灣卡在哪：成品單片路徑報單片，半成品路徑報那個 2CA 料（單一片在用就叫「SA 半成品」）。"""
    tw = r["tw"]
    if tw.get("sets_1ca", 0) > 0 or not tw.get("sub_parts"):
        b = next((c for c in tw.get("components") or [] if c["code"] == tw.get("bottleneck")), None)
        if not b:
            return []
        name, qty, need = f"{b['code']} 單片/piece", b["qty"], b["need"]
    else:
        s = next((x for x in tw["sub_parts"] if x["erp"] == tw.get("bottleneck")), None)
        if not s:
            return []
        users = [u["code"] for u in s.get("used_by") or []]
        name = f"{users[0]} 半成品/semi-finished" if len(users) == 1 else (s["desc"] or s["erp"])
        qty, need = s["qty"], s["per_set"]
    return [f"  卡在/bottleneck：{name}",
            f"  剩 {qty}，每組需 {need} / {qty} left, {need} per set"]


_HOW = {"ready": "現成/ready", "loose": "散片湊/loose", "split": "拆組/split-fill"}


def _alt_lines(r, alts, limit=4):
    """版型 A 的替代建議：316 先給同色 304，其餘列有貨顏色（美國可出｜台灣可組）。"""
    m = r["model"]
    head = ("⚠ 316 美國無貨，可改 304 / 316 not in US — 304 options:" if m.endswith("-316")
            else "⚠ 此色美國無貨，其他顏色 / not in US — other finishes:")
    out = [head]
    for a in alts[:limit]:
        tag = "304 同色/same color" if a["same_color_304"] else f"{a['finish']}（{a['color']}）"
        us = f"美國 US {a['us_ship']} 組" + (f"（{_HOW[a['us_ship_how']]}）"
                                            if a["us_ship_how"] and a["us_ship_how"] != "ready" else "")
        out.append(f"  {tag}：{us}｜台灣 TW {a['tw_sets']} 組")
    return out


def fmt_single(r, alts=None):
    m = r["model"]
    if not r["found"]:
        return f"{m}\n查無此型號 / not found"
    lines = [r["set"].get("name", m)]
    bom = " + ".join(f"{d['code']}×{d['count']}" for d in r["decomposition"])
    if bom:
        lines.append(f"= {bom}")

    us = r["us"]
    lines.append("")
    if us["set_stock"] and us["set_total"] > 0:
        wh = "、".join(f"{s['warehouse']} {s['qty']}" for s in us["set_stock"] if s["qty"] > 0)
        lines.append(f"美國 US：{us['set_total']} 組/sets（{wh}）")
    else:
        lines.extend(_us_reason(r))

    tw = r["tw"]
    lines.append("")
    if tw.get("unconfirmable"):
        miss = "、".join(tw.get("missing_erp") or [])
        lines.append("台灣 TW：無法確認/unconfirmable")
        lines.append(f"  {miss} 未建料號 / missing ERP")
    else:
        lines.append(f"台灣 TW：{tw['assemblable_sets']} 組/sets")
        lines.extend(_tw_reason(r))
    if tw.get("note"):
        lines.append(f"  ※ {tw['note']}")

    if alts:
        lines.append("")
        lines.extend(_alt_lines(r, alts))
    return "\n".join(lines)


def fmt_grid(data):
    if not data["found"]:
        return f"{data['base']}\n查無變體 / no variants"
    lines = [f"{data['base']} — 各顏色 / by finish（美國可出｜台灣可組）"]
    for r in data["rows"]:
        ip_disp = "無法確認/NA" if r.get("tw_unconfirmable") else f"{r['in_process']} 組"
        how = r.get("us_ship_how")
        us = f"{r.get('us_ship', r['us_total'])} 組" + (f"（{_HOW[how]}）" if how and how != "ready" else "")
        lines.append(f"{r['finish']}（{r['color']}）：美國 US {us}｜台灣 TW {ip_disp}")
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
    alts = []
    if r["found"] and ic.us_ship(r)[0] == 0 and not r["tw"].get("is_accessory") and not r["tw"].get("is_shim"):
        try:
            alts = ic.color_alternatives(model, catalog=cat, us_db=us, tw_db=tw, tw_sw_db=tw_sw)
        except Exception:
            alts = []   # 替代建議失敗不影響主結果
    return JSONResponse({"ok": True, "found": r["found"], "text": fmt_single(r, alts),
                         "data": r, "alternatives": alts})


@app.get("/colors")
def colors(model: str, key: str = "", role: str = "internal"):
    _check(key)
    base = ic.normalize_model(model)
    us, tw, tw_sw = _get_dbs()
    cat = _get_catalog(ic.family_of(base))
    data = ic.lookup_all_colors(base, catalog=cat, us_db=us, tw_db=tw, tw_sw_db=tw_sw)
    return JSONResponse({"ok": True, "found": data["found"], "text": fmt_grid(data), "data": data})
