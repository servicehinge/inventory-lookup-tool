#!/usr/bin/env python3
"""庫存查詢核心引擎：輸入型號 → 整合三地庫存；支援「任何顏色」跨色比較。

三個資料來源各自「載入一次、記憶體查多次」：
  - HubSpotCatalog：一次抓整個系列(如 K51M-400*)的 product，含 wh_configuration(拆解) + wh_erp(料號)。
  - USInventoryDB ：美國表（成品套組 + 零散零件，各倉）。
  - TWInventory   ：台灣 AU1 工廠庫存（用 ERP 料號比對，算可組裝組數）。

兩種查詢：
  - lookup(model)            單一顏色 → 三地明細。
  - lookup_all_colors(base)  任何顏色 → 顏色 × 倉庫 可出組數矩陣（最快出貨排前面）。

教學脈絡見 ../learning/K51M-400/NOTES.md。
"""
import os
import csv
import json
import ssl
import urllib.request
from collections import Counter

import re

import certifi

from us_inventory import USInventoryDB, to_int

# 顏色碼 → 友善名稱（對照 how-to-order；316=Marine grade 另計）
COLOR_NAMES = {"US32D": "Satin Stainless", "US19": "Flat Black", "695": "Dark Bronze",
               "US4": "Satin Brass", "US10": "Satin Bronze"}
# 「純顏色」變體：方角(SQ,名稱不含 14R/58R) + 304(名稱不含 -316)，finish 就是單一顏色碼
_PURE_COLOR = re.compile(r"^(US\d+[A-Z]?|\d{3})$")

HUBSPOT_TOKEN = os.environ.get("HUBSPOT_API_TOKEN", "")
TW_SHEET_ID = os.environ.get("TW_SHEET_ID", "1UrNy8UHg3BlY5YrxmJd4urzkTgQ2_eCq_UO_Wzwbb5c")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
_SSL = ssl.create_default_context(cafile=certifi.where())


# Swing Clear：K51L-SWRH / K51L SWRH / K51LSWLH … 全部視為黏在一起的單一 token K51LSWRH，
# 且 LH 一律當 RH 查（庫存只有 RH）。正規化後型號結構就跟 K51M 同構（前綴-尺寸-功能-色）。
_SW_RE = re.compile(r"K51L[\s-]*SW(?:RH|LH)", re.I)


def normalize_model(model):
    """正規化型號：把 swing clear 的各種寫法（含「swing clear」文字、K51L-SWRH/SWLH）收斂成 K51LSWRH，
    並把型號內的空白統一成連字號（讓 `swing clear 545 A3 US32D` 這種空白寫法也能解析）。"""
    m = (model or "").strip().upper()
    # 「swing clear」字樣（可含 K51L 前綴、RH/LH 尾）→ K51LSWRH
    m = re.sub(r"(?:K51L[\s-]*)?SWING[\s-]*CLEAR(?:[\s-]*(?:RH|LH))?", "K51LSWRH", m)
    m = _SW_RE.sub("K51LSWRH", m)
    m = re.sub(r"\s*-\s*", "-", m)   # 連字號旁空白
    m = re.sub(r"\s+", "-", m)       # 其餘空白 → 連字號
    return m


def is_swing_clear(model):
    return "K51LSWRH" in normalize_model(model)


def finish_color(model):
    """抓型號的顏色碼（US32D / 695 …，忽略尾端 -316 鋼級）。"""
    m = re.search(r"-(US\d+[A-Z]?|\d{3})(?:-316)?$", normalize_model(model))
    return m.group(1) if m else ""


def family_of(model):
    """型號 → 系列前綴（抓前兩段）。K51M-400-A3-US32D → K51M-400；K51LSWRH-450-A3-US32D → K51LSWRH-450。"""
    parts = normalize_model(model).split("-")
    return "-".join(parts[:2]) if len(parts) >= 2 else normalize_model(model)


# ---------- HubSpot：整個系列一次載入 ----------
class HubSpotCatalog:
    """一次抓 family*（如 K51M-400*）所有 product 進記憶體。之後拆解/找料號全在本機。"""

    def __init__(self, family, token=None):
        self.family = family.upper()
        self.token = token or HUBSPOT_TOKEN
        self.by_name = {}
        self.by_sku = {}
        for term in self._search_terms(self.family):
            self._load(term)

    def _search_terms(self, family):
        """swing clear 的 product 名稱在 HubSpot 同時有 K51LSWRH-… 與 K51L-SWRH-… 兩種寫法，
        CONTAINS_TOKEN 的 token 切法不同，兩種都搜才不漏。"""
        terms = [family]
        if "K51LSWRH" in family:
            terms.append(family.replace("K51LSWRH", "K51L-SWRH"))
        return terms

    def _load(self, family):
        after = None
        while True:
            body = {"filterGroups": [{"filters": [
                {"propertyName": "name", "operator": "CONTAINS_TOKEN", "value": family + "*"}]}],
                "properties": ["name", "hs_sku", "wh_erp", "wh_configuration"], "limit": 100}
            if after:
                body["after"] = after
            req = urllib.request.Request(
                "https://api.hubapi.com/crm/v3/objects/products/search",
                data=json.dumps(body).encode(),
                headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"},
                method="POST")
            with urllib.request.urlopen(req, context=_SSL, timeout=30) as r:
                resp = json.loads(r.read())
            for it in resp.get("results", []):
                p = it["properties"]
                if p.get("name"):
                    nm = p["name"].strip().upper()
                    self.by_name[nm] = p
                    self.by_name[normalize_model(nm)] = p  # 同時存正規化 key，dash/無dash 都對得上
                if p.get("hs_sku"):
                    sk = p["hs_sku"].strip().upper()
                    self.by_sku[sk] = p
                    self.by_sku[normalize_model(sk)] = p
            after = resp.get("paging", {}).get("next", {}).get("after")
            if not after:
                break

    def _find(self, model):
        m = (model or "").strip().upper()
        nm = normalize_model(m)
        return (self.by_name.get(m) or self.by_name.get(nm)
                or self.by_sku.get(m) or self.by_sku.get(nm))

    def get_bom(self, model):
        """回 {found, set:{name,sku}, components:[{code,count,model,sku,erp}], note}。"""
        p = self._find(model)
        if not p:
            return {"found": False, "note": f"HubSpot 找不到 {model}", "set": {}, "components": []}
        set_name = p.get("name") or model
        config = (p.get("wh_configuration") or "").strip()
        out = {"found": True, "set": {"name": set_name, "sku": p.get("hs_sku")}, "components": [], "note": ""}
        if not config or "." not in config:
            out["components"] = [{"code": config or set_name, "count": 1, "model": set_name,
                                  "sku": p.get("hs_sku"), "erp": p.get("wh_erp")}]
            return out
        counts = Counter(c.strip() for c in config.split(".") if c.strip())
        # 用正規化型號定位功能碼（swing clear 多一段 SWRH，正規化後黏成單 token → parts[2] 仍是功能碼）
        nname = normalize_model(set_name)
        parts = nname.split("-")
        func = parts[2] if len(parts) > 2 else ""
        for code, cnt in counts.items():
            comp_model = nname.replace(f"-{func}-", f"-{code}-", 1) if func else nname
            cp = self._find(comp_model)
            out["components"].append({"code": code, "count": cnt, "model": comp_model,
                                      "sku": cp.get("hs_sku") if cp else None,
                                      "erp": cp.get("wh_erp") if cp else None})
        return out

    def list_variants(self, base, colors_only=True):
        """base 如 K51M-400-A3 → 該套組變體。colors_only=True 只留純顏色（方角 SQ + 304），
        排除圓角(14R/58R)與 316 海洋級——客戶不挑色時的預設。"""
        base = base.strip().upper()
        seen, out = set(), []
        for name, p in self.by_name.items():
            if not name.startswith(base + "-"):
                continue
            if "OR" in name or "TBD" in name:
                continue
            if "." not in (p.get("wh_configuration") or ""):
                continue  # 只要套組
            finish = name[len(base) + 1:]
            if colors_only and not _PURE_COLOR.match(finish):
                continue
            if finish in seen:
                continue
            seen.add(finish)
            out.append({"finish": finish, "model": name, "color": COLOR_NAMES.get(finish, finish)})
        return out


# ---------- 台灣 AU1 ----------
def _tw_rows():
    csv_path = os.environ.get("TW_CSV")
    if csv_path and os.path.exists(csv_path):
        with open(csv_path, encoding="utf-8") as f:
            return list(csv.reader(f))
    import gspread
    from google.oauth2.service_account import Credentials
    key = os.environ.get("GOOGLE_SA_KEY",
                         "/Users/weichuchen/Desktop/03Project/PC202602 ask for shipping quote/shipping-quote-486901-dbd435d38327.json")
    gc = gspread.authorize(Credentials.from_service_account_file(key, scopes=SCOPES))
    return gc.open_by_key(TW_SHEET_ID).sheet1.get_all_values()


class TWInventory:
    """料號 → {name, safety, qty}。欄位：4料號 5名稱1 9安全存量 10庫存量。"""

    def __init__(self):
        self.by_erp = {}
        for r in _tw_rows():
            if len(r) < 10 or not r[3].strip() or r[3].strip() == "料號":
                continue
            self.by_erp[r[3].strip()] = {"name": r[4].strip(), "safety": to_int(r[8]), "qty": to_int(r[9])}

    def get(self, erp):
        return self.by_erp.get(erp)


# ---------- 台灣 AU1：Swing Clear 專用（工廠用 K51SW 命名，HubSpot ERP 殘缺，改解析工廠名稱）----------
# 工廠名稱範例：K51SW-6*4.5*4.5"-RH-SA-霧面 / K51SW-6*5*4.5"-RH-SA1-黑色
_TWSW_RE = re.compile(r'^K51SW-\d+\*([\d.]+)\*([\d.]+)"?-RH-([A-Z0-9]+)-(.+)$', re.I)
# 葉片尺寸 (高*寬) → 銷售尺寸碼
_SIZE_BY_DIMS = {("4.5", "4.5"): "450", ("5", "4.5"): "545"}
# 工廠中文色 → 表面碼（無字母霧面=US32D、A古銅=695、B黑=US19、G金=US4）
SW_COLORS = ["US32D", "US19", "695", "US4"]


def _sw_color_code(zh):
    if "霧面" in zh:
        return "US32D"
    if "古銅" in zh:
        return "695"
    if "黑" in zh:
        return "US19"
    if "金" in zh:  # 金色 / P.V.D 金
        return "US4"
    return None


class TWSwingClear:
    """工廠表 K51SW 列 → (尺寸, 單片碼, 顏色碼) → 庫存。一次解析、記憶體查；天生含全顏色 → 缺色提示。"""

    def __init__(self, rows=None):
        self.idx = {}
        for r in (rows or _tw_rows()):
            if len(r) < 10:
                continue
            m = _TWSW_RE.match(r[4].strip())
            if not m:
                continue
            size = _SIZE_BY_DIMS.get((m.group(1), m.group(2)))
            color = _sw_color_code(m.group(4))
            if not size or not color:
                continue
            self.idx[(size, m.group(3).upper(), color)] = {
                "qty": to_int(r[9]), "safety": to_int(r[8]), "name": r[4].strip(), "erp": r[3].strip()}

    def get(self, size, piece, color):
        return self.idx.get((size, piece, color))

    def assemblable(self, size, components, color):
        """給定一組 components(code,count) 與顏色，回 (可組數, 各片明細)；任一片缺料則該色可組數受限。"""
        sets, comps = None, []
        for c in components:
            rec = self.get(size, c["code"], color)
            qty = rec["qty"] if rec else 0
            cap = qty // c["count"] if c["count"] else 0
            sets = cap if sets is None else min(sets, cap)
            comps.append({"code": c["code"], "count": c["count"], "qty": qty,
                          "safety": rec["safety"] if rec else 0, "erp": rec["erp"] if rec else None,
                          "below_safety": bool(rec) and rec["qty"] < rec["safety"]})
        return (sets or 0), comps


# ---------- 單一顏色 ----------
def lookup(model, catalog=None, us_db=None, tw_db=None, tw_sw_db=None):
    model = normalize_model(model)
    catalog = catalog or HubSpotCatalog(family_of(model))
    bom = catalog.get_bom(model)
    result = {"model": model, "found": bom["found"], "note": bom.get("note", ""),
              "set": bom.get("set", {}), "decomposition": [], "us": {}, "tw": {}}
    if not bom["found"]:
        return result
    us_db = us_db or USInventoryDB()
    set_name = bom["set"].get("name", model)

    result["decomposition"] = [{"code": c["code"], "count": c["count"], "model": c["model"],
                                "sku": c["sku"], "erp": c["erp"]} for c in bom["components"]]

    # 美國：成品 + 零散零件
    set_us = us_db.lookup(set_name)
    set_stock = [{"warehouse": s["warehouse"], "qty": to_int(s["qty"])} for s in set_us["stock"]]
    comp_us = []
    for c in bom["components"]:
        if c["model"] == set_name:
            continue
        r = us_db.lookup(c["model"])
        rows = [{"warehouse": s["warehouse"], "qty": to_int(s["qty"])} for s in r["stock"] if to_int(s["qty"]) > 0]
        if rows:
            comp_us.append({"code": c["code"], "model": c["model"], "stock": rows,
                            "total": sum(x["qty"] for x in rows)})
    result["us"] = {"set_stock": set_stock, "set_total": sum(x["qty"] for x in set_stock),
                    "components": comp_us, "alt_colors": []}

    sw = is_swing_clear(model)
    color = finish_color(set_name) or finish_color(model)

    if sw:
        # Swing Clear：台灣靠工廠 K51SW 名稱解析（HubSpot ERP 殘缺），順便拿全顏色 → 缺色提示。
        # 工廠 K51SW 全是 304；316 海洋級客製品台灣無料 → 不可拿 304 庫存冒充，直接 0（缺色提示也僅限同為 304 的顏色）。
        size = normalize_model(set_name).split("-")[1]
        is_316 = normalize_model(set_name).endswith("-316") or normalize_model(model).endswith("-316")
        tw_sw_db = tw_sw_db or TWSwingClear()
        if is_316:
            result["tw"] = {"components": [{"code": c["code"], "erp": None, "name": None, "qty": 0,
                                            "safety": 0, "need": c["count"], "below_safety": False}
                                           for c in bom["components"]],
                            "assemblable_sets": 0, "bottleneck": None, "low_stock": [],
                            "alt_colors": [], "note": "316 海洋級客製品，台灣無常備庫存"}
            result["us"]["alt_colors"] = []
            return result
        sets_possible, comps = tw_sw_db.assemblable(size, bom["components"], color)
        tw_comps = [{"code": c["code"], "erp": c["erp"], "name": None, "qty": c["qty"],
                     "safety": c["safety"], "need": c["count"], "below_safety": c["below_safety"]} for c in comps]
        low = [c["code"] for c in comps if c["below_safety"]]
        bottleneck = (min(comps, key=lambda x: x["qty"] // x["count"] if x["count"] else 0)["code"]
                      if comps else None)
        alt = []
        for ac in SW_COLORS:
            if ac == color:
                continue
            s2, _ = tw_sw_db.assemblable(size, bom["components"], ac)
            if s2 > 0:
                alt.append({"color": ac, "color_name": COLOR_NAMES.get(ac, ac), "sets": s2})
        alt.sort(key=lambda x: x["sets"], reverse=True)
        result["tw"] = {"components": tw_comps, "assemblable_sets": sets_possible,
                        "bottleneck": bottleneck, "low_stock": low, "alt_colors": alt}

        # 美國缺色提示：其他顏色的成品套組現貨
        nname = normalize_model(set_name)
        us_alt = []
        for ac in SW_COLORS:
            if ac == color or not color:
                continue
            r = us_db.lookup(nname.replace(f"-{color}", f"-{ac}", 1))
            by_wh = {s["warehouse"]: to_int(s["qty"]) for s in r["stock"] if to_int(s["qty"]) > 0}
            if by_wh:
                us_alt.append({"color": ac, "color_name": COLOR_NAMES.get(ac, ac),
                               "total": sum(by_wh.values()), "by_wh": by_wh})
        us_alt.sort(key=lambda x: x["total"], reverse=True)
        result["us"]["alt_colors"] = us_alt
        return result

    # 台灣（非 swing clear）：用 HubSpot ERP 料號比對工廠表，算可組裝組數
    tw_db = tw_db or TWInventory()
    tw_comps, sets_possible, low = [], None, []
    for c in bom["components"]:
        rec = tw_db.get(c["erp"]) if c["erp"] else None
        qty = rec["qty"] if rec else 0
        safety = rec["safety"] if rec else 0
        need = c["count"]
        tw_comps.append({"code": c["code"], "erp": c["erp"], "name": rec["name"] if rec else None,
                         "qty": qty, "safety": safety, "need": need,
                         "below_safety": bool(rec) and qty < safety})
        cap = qty // need if need else 0
        sets_possible = cap if sets_possible is None else min(sets_possible, cap)
        if rec and qty < safety:
            low.append(c["code"])
    bottleneck = min(tw_comps, key=lambda x: x["qty"] // x["need"] if x["need"] else 0)["code"] if tw_comps else None
    result["tw"] = {"components": tw_comps, "assemblable_sets": sets_possible or 0,
                    "bottleneck": bottleneck, "low_stock": low, "alt_colors": []}
    return result


# ---------- 任何顏色：顏色 × 倉庫 矩陣 ----------
def lookup_all_colors(base, catalog=None, us_db=None, tw_db=None, tw_sw_db=None):
    base = normalize_model(base)
    catalog = catalog or HubSpotCatalog(family_of(base))
    us_db = us_db or USInventoryDB()
    if is_swing_clear(base):
        tw_sw_db = tw_sw_db or TWSwingClear()
    else:
        tw_db = tw_db or TWInventory()
    variants = catalog.list_variants(base)
    rows = []
    for v in variants:
        r = lookup(v["model"], catalog=catalog, us_db=us_db, tw_db=tw_db, tw_sw_db=tw_sw_db)
        if not r["found"]:
            continue
        us_by_wh = {x["warehouse"]: x["qty"] for x in r["us"]["set_stock"]}
        rows.append({
            "finish": v["finish"], "color": v.get("color", v["finish"]), "model": v["model"],
            "us_by_wh": us_by_wh, "us_total": r["us"]["set_total"],
            "in_process": r["tw"]["assemblable_sets"], "tw_low": bool(r["tw"]["low_stock"]),
            "bottleneck": r["tw"]["bottleneck"],
            "tw_components": r["tw"]["components"],   # 每色的 SA/SA1 料號+台灣庫存
            "us_loose": r["us"]["components"],         # 美國零散零件
        })
    # 排序：美國現成多的在前，其次製程中多的
    rows.sort(key=lambda x: (x["us_total"], x["in_process"]), reverse=True)
    # 有貨的倉（任一顏色 >0）才當欄位
    whs = set()
    for r in rows:
        for w, q in r["us_by_wh"].items():
            if q > 0:
                whs.add(w)
    return {"base": base, "found": bool(rows), "rows": rows, "warehouses": sorted(whs)}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--all-colors", action="store_true")
    a = ap.parse_args()
    out = lookup_all_colors(a.model) if a.all_colors else lookup(a.model)
    print(json.dumps(out, ensure_ascii=False, indent=2))
