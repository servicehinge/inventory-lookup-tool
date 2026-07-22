#!/usr/bin/env python3
"""訂單通知加值引擎（bucket A）：一封訂單信 → TG/LINE 兩版「新訂單＋庫存＋客戶背景」通知。

輸入用 fixture JSON（Gmail message 的 sender/subject/date/message_id/plaintext_body/html_body），
之後接 24h 機器上的 daemon 時，把 fixture 換成即時 Gmail 抓取即可（parse/庫存/背景/render 全部重用）。

  python order_enrich.py --fixture fixtures/shopify_2150.json
"""
import os
import re
import sys
import json
import argparse

import inventory_core as inv
import customer_bg

# 電商品牌關鍵字 → 免比對 rep（對齊 PC20260610 hubspot_audit ECOMM_NAME_KEYWORDS）
ECOMM_KEYWORDS = ["closerhinge", "hinge outlet", "hingeoutlet", "doorhardwareusa", "softclosing"]
# 真正的產品 SKU 前綴（濾掉 shipping / tariff 等假列）
PRODUCT_SKU_RE = re.compile(r"^(K51|W41)", re.I)
# 收件州偏好 CA 倉的州（西部+中部）；其餘偏好 MI
CA_PREF_STATES = {"WA", "OR", "CA", "NV", "AZ", "ID", "UT", "MT", "WY", "CO", "NM",
                  "TX", "OK", "AK", "HI", "ND", "SD", "NE", "KS", "MN", "IA", "MO", "AR", "LA"}
STATE_ABBR = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD", "massachusetts": "MA",
    "michigan": "MI", "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}


# ---------------- 解析 ----------------
def _state_abbr(s):
    s = (s or "").strip()
    if len(s) == 2 and s.upper() in {v for v in STATE_ABBR.values()}:
        return s.upper()
    return STATE_ABBR.get(s.lower(), s)


def sku_to_model(sku):
    """SKU → 引擎吃的 model：去角型碼 SQ、去預設鋼級 304（保留 316 海洋級）。"""
    m = sku.strip().upper().replace("-SQ-", "-")
    m = re.sub(r"-304$", "", m)
    return m


def detect_source(msg):
    subj = (msg.get("subject") or "").lower()
    body = (msg.get("plaintext_body") or "") + (msg.get("html_body") or "")
    if re.search(r"order\s+#\d+", subj) and ("shopify" in body.lower() or "order summary" in body.lower()):
        return "shopify"
    if "purchase order" in subj or re.search(r"\bPO[- ]?\d+", subj):
        return "pdf_po"
    if "order summary" in body.lower():
        return "shopify"
    return "unknown"


def _shopify_items(html, text):
    """先試 HTML 的 order-list__item 區塊，無則退回 plaintext。只留真產品 SKU（K51/W41）。"""
    items = []
    if "order-list__item__cell" in html:
        for block in re.split(r"order-list__item__cell", html)[1:]:
            sku = re.search(r"SKU:\s*([A-Za-z0-9\-]+)", block)
            if not sku or not PRODUCT_SKU_RE.match(sku.group(1)):
                continue
            title = re.search(r"order-list__item-title[^>]*>\s*([^<]+?)\s*</span>", block)
            qty = re.search(r"\$[\d,.]+\s*[×xX]\s*(\d+)", block)
            color = re.search(r"order-list__item-variant[^>]*>\s*([^<•]+?)\s*</span>", block)
            items.append({"sku": sku.group(1), "model": sku_to_model(sku.group(1)),
                          "qty": int(qty.group(1)) if qty else 1,
                          "title": title.group(1).strip() if title else "",
                          "color": color.group(1).strip() if color else ""})
        if items:
            return items
    # plaintext 備援：每個「[色] • SKU: xxx」往前找最近的「$價 × 數量」
    for m in re.finditer(r"(?:([A-Za-z0-9 .()\-]+?)\s*•\s*)?SKU:\s*([A-Za-z0-9\-]+)", text):
        color, sku = m.group(1), m.group(2)
        if not PRODUCT_SKU_RE.match(sku):
            continue
        pre = text[:m.start()]
        q = re.findall(r"\$[\d,.]+\s*[×xX]\s*(\d+)", pre)
        items.append({"sku": sku, "model": sku_to_model(sku),
                      "qty": int(q[-1]) if q else 1, "title": "",
                      "color": (color or "").strip()})
    return items


def parse_shopify(msg):
    html = msg.get("html_body") or ""
    text = msg.get("plaintext_body") or ""
    subj = msg.get("subject") or ""
    o = {"source": "shopify", "order_no": None, "customer_name": None, "customer_email": None,
         "company": None, "items": [], "ship_state": None, "ship_city": None, "amount": None,
         "blind": False}
    m = re.search(r"#(\d+)", subj) or re.search(r"order\s+#(\d+)", text, re.I)
    o["order_no"] = "#" + m.group(1) if m else None
    m = re.search(r"placed by (.+?)\s*$", subj) or re.search(r"^(.+?)\s+placed order", text.strip())
    if m:
        o["customer_name"] = m.group(1).strip()
    o["items"] = _shopify_items(html, text)
    # 收件地（plaintext 的 Shipping address 區塊）
    m = re.search(r"Shipping address\s*(.+?)(?:United States|USA)", text, re.S | re.I)
    if m:
        blk = m.group(1)
        loc = re.search(r"([A-Za-z .]+),\s*\n?\s*([A-Za-z ]+?)\s*\n?\s*(\d{5})", blk)
        if loc:
            o["ship_city"] = loc.group(1).strip()
            o["ship_state"] = _state_abbr(loc.group(2))
    m = re.search(r"Total[\s\S]{0,20}?\$([\d,]+\.\d{2})\s*USD", text)
    if m:
        o["amount"] = m.group(1)
    ph = re.search(r"United States\s*\n*\s*(\d[\d\-() ]{8,})", text, re.I)
    if ph:
        o["phone"] = re.sub(r"\D", "", ph.group(1))
    # 電商公司：從店名/subject
    o["company"] = _brand_from(subj + " " + (msg.get("sender") or ""))
    return o


def parse_pdf_po(msg):
    text = msg.get("plaintext_body") or ""
    subj = msg.get("subject") or ""
    o = {"source": "pdf_po", "order_no": None, "customer_name": None, "customer_email": None,
         "company": None, "items": [], "ship_state": None, "ship_city": None, "amount": None,
         "blind": bool(re.search(r"blind shipment", text, re.I))}
    m = re.search(r"\bPO[- ]?(\d+)", subj) or re.search(r"PO[- ]?(\d+)", text)
    o["order_no"] = "PO-" + m.group(1) if m else None
    # 客戶公司 & email
    o["company"] = _brand_from(subj)
    m = re.search(r"([\w.\-]+@[\w.\-]+)", text)  # 第一個 email（信裡通常是客戶 CC/寄件）
    em = re.findall(r"([\w.\-]+@[\w.\-]+)", text)
    for e in em:
        if any(k in e.lower() for k in ["hingeoutlet", "closerhinge", "doorhardwareusa"]):
            o["customer_email"] = e
            break
    # 品項：Reorder Unit Quantity + Product + SKU 三段一組（支援多段）
    for seg in re.split(r"(?=Reorder Unit Quantity)", text):
        sku = re.search(r"SKU:\s*([A-Za-z0-9\-]+)", seg)
        if not sku or not PRODUCT_SKU_RE.match(sku.group(1)):
            continue
        qty = re.search(r"Reorder Unit Quantity:\s*(\d+)", seg)
        prod = re.search(r"Product:\s*(.+)", seg)
        o["items"].append({
            "sku": sku.group(1), "model": sku_to_model(sku.group(1)),
            "qty": int(qty.group(1)) if qty else 1,
            "title": prod.group(1).strip() if prod else "", "color": ""})
    # 收件地：Shipping: <name> ... City, State Zip
    m = re.search(r"Shipping:\s*(.+?)(?:UNITED STATES|USA|$)", text, re.S | re.I)
    if m:
        blk = m.group(1)
        nm = re.search(r"Shipping:\s*(.+)", text)
        if nm:
            o["ship_name"] = nm.group(1).strip()
        loc = re.search(r"([A-Za-z .]+),\s*([A-Za-z ]+?)\s+(\d{5})", blk)
        if loc:
            o["ship_city"] = loc.group(1).strip()
            o["ship_state"] = _state_abbr(loc.group(2))
    return o


def _brand_from(s):
    s = (s or "").lower()
    for k in ["closerhinge", "hinge outlet", "hingeoutlet", "doorhardwareusa", "softclosing"]:
        if k in s:
            return {"closerhinge": "closerhinge", "hinge outlet": "Hinge Outlet",
                    "hingeoutlet": "Hinge Outlet", "doorhardwareusa": "doorhardwareusa",
                    "softclosing": "closerhinge"}[k]
    return None


def is_ecommerce(order, msg):
    hay = " ".join([order.get("company") or "", order.get("customer_email") or "",
                    msg.get("subject") or "", msg.get("sender") or ""]).lower()
    return any(k in hay for k in ECOMM_KEYWORDS)


def parse_order(msg):
    src = detect_source(msg)
    if src == "shopify":
        return parse_shopify(msg)
    if src == "pdf_po":
        return parse_pdf_po(msg)
    return {"source": "unknown", "items": [], "order_no": None}


# ---------------- 庫存判斷 ----------------
def _suggest_wh(set_stock, ship_state):
    """有貨倉中依收件州偏好挑一個當建議出貨倉（排除 Amazon marketplace）。"""
    avail = [s for s in set_stock if inv.to_int(s["qty"]) > 0 and "amazon" not in s["warehouse"].lower()]
    if not avail:
        return None
    pref = "CA" if (ship_state in CA_PREF_STATES) else "MI"
    prefer = [s for s in avail if s["warehouse"].upper().startswith(pref)]
    pool = prefer or avail
    return max(pool, key=lambda s: inv.to_int(s["qty"]))


def assess_item(item, ship_state, catalog=None, us_db=None, tw_db=None, tw_sw=None):
    r = inv.lookup(item["model"], catalog=catalog, us_db=us_db, tw_db=tw_db, tw_sw_db=tw_sw)
    qty = item["qty"]
    a = {"item": item, "found": r["found"], "us_total": 0, "us_by_wh": [], "us_ok": False,
         "tw_sets": 0, "tw_ok": False, "tw_low": False, "suggest": None, "verdict": "unknown"}
    if not r["found"]:
        a["verdict"] = "notfound"
        return a
    us_stock = r["us"]["set_stock"]
    a["us_total"] = r["us"]["set_total"]
    a["us_by_wh"] = [{"wh": s["warehouse"], "qty": inv.to_int(s["qty"])} for s in us_stock if inv.to_int(s["qty"]) > 0]
    a["us_ok"] = a["us_total"] >= qty
    a["tw_unconf"] = bool(r["tw"].get("unconfirmable"))
    a["missing_erp"] = r["tw"].get("missing_erp") or []
    a["tw_sets"] = None if a["tw_unconf"] else r["tw"].get("assemblable_sets", 0)
    a["tw_ok"] = (a["tw_sets"] is not None) and (a["tw_sets"] >= qty)
    a["tw_low"] = bool(r["tw"].get("low_stock"))
    a["tw_components"] = r["tw"].get("components", [])
    a["us_split"] = r["us"].get("decomposed")  # 美國無完整套組時的『拆組出貨』選項（或 None）
    sug = _suggest_wh(us_stock, ship_state)
    a["suggest"] = sug["warehouse"] if sug else None
    if a["us_ok"]:
        a["verdict"] = "us_ok"
    elif a["us_total"] > 0:
        a["verdict"] = "us_partial"
    elif a["tw_unconf"]:
        a["verdict"] = "tw_unconfirmable"
    elif a["tw_ok"]:
        a["verdict"] = "tw_only"
    elif a["tw_sets"] and a["tw_sets"] > 0:
        a["verdict"] = "tw_partial"
    else:
        a["verdict"] = "none"
    return a


# ---------------- 產出訊息 ----------------
_VERDICT_HEAD = {
    "us_ok": "庫存 ✅ 充足", "us_partial": "庫存 ⚠️ 美國不足", "tw_only": "庫存 ⚠️ 美國無現貨",
    "tw_partial": "庫存 ⚠️ 缺料", "tw_unconfirmable": "庫存 ⚠️ 需確認（缺料號）",
    "none": "庫存 ❌ 無現貨", "notfound": "庫存 ❓ 查無型號",
    "unknown": "庫存 ❓",
}
_VERDICT_RANK = ["us_ok", "us_partial", "tw_only", "tw_partial", "tw_unconfirmable",
                 "none", "notfound", "unknown"]


def _worst(verdicts):
    for v in reversed(_VERDICT_RANK):
        if v in verdicts:
            return v
    return "unknown"


def _split_line(a, qty):
    """美國拆組出貨提示行（完整套組美國無貨、但可用較小套組＋散片湊出時）。無則回 None。"""
    dec = a.get("us_split")
    if not dec:
        return None
    via = dec["via"]
    ex = "＋".join(e["code"] for e in dec["extra_pieces"])
    enough = "夠出" if dec["sets"] >= qty else "不足"
    return (f"　美國可拆組 {dec['sets']} 組（{via['code']} 套組＋散片 {ex}）→ "
            f"仍可美國出貨（{enough} {qty}）")


def render(order, assessments, bg, msg):
    lines = []
    overall = _worst([a["verdict"] for a in assessments]) if assessments else "notfound"
    lines.append("🧾 新訂單｜" + _VERDICT_HEAD.get(overall, "庫存 ❓"))
    lines.append("─────────────")
    # 客戶
    cust = order.get("company") or order.get("customer_name") or "（未知）"
    tag = "電商客戶・免比對 rep" if is_ecommerce(order, msg) else None
    lines.append("客戶：" + cust + (f"（{tag}）" if tag else ""))
    if order.get("customer_name") and order.get("customer_name") != cust:
        lines.append("　　　" + order["customer_name"])
    if bg and bg.get("summary"):
        lines.append("背景：" + bg["summary"])
    if order.get("order_no"):
        src_label = "Shopify" if order["source"] == "shopify" else "PDF PO"
        lines.append("訂單：" + order["order_no"] + f"（{src_label}）")
    lines.append("時間：" + (msg.get("when_taipei") or msg.get("date") or ""))
    if order.get("ship_city") or order.get("ship_state"):
        loc = ", ".join([x for x in [order.get("ship_city"), order.get("ship_state")] if x])
        lines.append("收件：" + loc)
    if order.get("amount"):
        lines.append("金額：US$" + order["amount"])
    if order.get("blind"):
        lines.append("備註：需 blind shipment（不附價格）")
    lines.append("")
    # 品項 × 庫存
    for a in assessments:
        it = a["item"]
        color = f" / {it['color']}" if it.get("color") else ""
        lines.append(f"品項：{it['model']}{color}　× {it['qty']} 組")
        if a["verdict"] == "notfound":
            lines.append("　查無此型號庫存")
            continue
        if a["us_ok"]:
            wh = " ｜ ".join(f"{w['wh']} {w['qty']}" for w in a["us_by_wh"])
            lines.append(f"　美國現成 {a['us_total']} 組（需 {it['qty']}）→ 充足")
            lines.append("　" + wh)
            if a["suggest"]:
                lines.append(f"　建議出貨：{a['suggest']}（離收件地較近）")
        elif a["us_total"] > 0:
            wh = " ｜ ".join(f"{w['wh']} {w['qty']}" for w in a["us_by_wh"])
            lines.append(f"　美國 {a['us_total']} 組（需 {it['qty']}）→ 不足　{wh}")
            if a.get("tw_unconf"):
                miss = "、".join(a.get("missing_erp") or [])
                lines.append(f"　台灣 AU1：⚠️ 可組數無法確認（單片 {miss} 未建料號）")
            else:
                lines.append(f"　台灣 AU1 可再組 {a['tw_sets']} 組" + ("（⚠️ 有料件低於安全量）" if a["tw_low"] else ""))
        elif a.get("tw_unconf"):
            lines.append(f"　美國 0 組（需 {it['qty']}）→ ❌ 無現貨")
            sl = _split_line(a, it["qty"])
            if sl:
                lines.append(sl)
            miss = "、".join(a.get("missing_erp") or [])
            lines.append(f"　台灣 AU1：⚠️ 無法確認可組數——單片 {miss} 未建料號，庫存查不到；需補料號後再判斷")
            comp = a.get("tw_components") or []
            if comp:
                lines.append("　" + " ｜ ".join(
                    (f"{c['code']} 無料號" if c.get("unverifiable") else f"{c['code']} {c['qty']}/{c['safety']}")
                    for c in comp))
        else:
            lines.append(f"　美國 0 組（需 {it['qty']}）→ ❌ 無現貨")
            sl = _split_line(a, it["qty"])
            if sl:
                lines.append(sl)
            status = f"夠出 {it['qty']}" if a["tw_ok"] else "不足"
            low = "，⚠️ 有料件低於安全量" if a["tw_low"] else ""
            lines.append(f"　台灣 AU1：可組 {a['tw_sets']} 組（{status}{low}）→ 走台灣直送")
            comp = a.get("tw_components") or []
            if comp:
                lines.append("　" + " ｜ ".join(f"{c['code']} {c['qty']}/{c['safety']}" for c in comp))
    lines.append("")
    mid = msg.get("message_id") or ""
    if mid:
        lines.append("🔗 https://mail.google.com/mail/u/0/#all/" + mid)
    text = "\n".join(lines)
    return {"line": text, "telegram": text}  # 兩版同內容；TG 之後可包 <b> 粗體


# ---------------- 主流程 ----------------
def enrich(msg, catalog_cache=None):
    order = parse_order(msg)
    if not order.get("items"):
        note = "（信件本文無可解析型號" + ("；型號可能在 PDF 附件" if order.get("source") == "pdf_po" else "") + "）"
        return {"order": order, "text": "🧾 新訂單｜庫存 ❓ 未自動查\n" + note, "line": None, "telegram": None}
    us_db = inv.USInventoryDB()
    tw_db = inv.TWInventory()
    tw_sw = None
    assessments = []
    cat_by_family = {}
    for it in order["items"]:
        fam = inv.family_of(it["model"])
        cat = cat_by_family.get(fam) or inv.HubSpotCatalog(fam)
        cat_by_family[fam] = cat
        if inv.is_swing_clear(it["model"]) and tw_sw is None:
            tw_sw = inv.TWSwingClear()
        assessments.append(assess_item(it, order.get("ship_state"), catalog=cat, us_db=us_db,
                                       tw_db=tw_db, tw_sw=tw_sw))
    # 客戶背景
    try:
        bg = customer_bg.background(name=order.get("customer_name"), email=order.get("customer_email"),
                                    phone=order.get("phone"), current_order_no=order.get("order_no"))
    except Exception as e:
        bg = {"summary": f"（背景查詢略過：{e.__class__.__name__}）"}
    out = render(order, assessments, bg, msg)
    out["order"] = order
    out["bg"] = bg
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", required=True)
    a = ap.parse_args()
    with open(a.fixture, encoding="utf-8") as f:
        msg = json.load(f)
    res = enrich(msg)
    print(res.get("line") or res.get("text"))
