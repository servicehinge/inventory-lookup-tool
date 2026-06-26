#!/usr/bin/env python3
"""Waterson Inventory Lookup — Streamlit 前端（簡潔、工作取向）。

身分（email 判斷）：
  - 內部 @watersonusa.com → 中英並列，看完整三地（料號、可組數、安全存量示警）。
  - 經銷商（其他 email）   → 純英文（美式）。美國各倉給數字；台灣不露「Taiwan/倉別」，
                            只給 "In process" 的可再做數量 + lead time ~1–2 weeks。

兩種查詢：
  - 打到顏色（K51M-400-A3-US32D）→ 單色明細。
  - 只打基礎型號（K51M-400-A3）或勾「Any color」→ 顏色 × 倉庫 可出組數表（最快出貨在前）。
"""
import os
import json
import tempfile

import streamlit as st


def _boot_secrets():
    try:
        if "HUBSPOT_API_TOKEN" in st.secrets:
            os.environ["HUBSPOT_API_TOKEN"] = st.secrets["HUBSPOT_API_TOKEN"]
        for k in ("STAFF_PASSWORD", "DEALER_PASSWORD"):
            if k in st.secrets:
                os.environ[k] = st.secrets[k]
        if "gcp_service_account" in st.secrets and not os.environ.get("GOOGLE_SA_KEY"):
            _sa = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
            json.dump(dict(st.secrets["gcp_service_account"]), _sa)
            _sa.flush()
            os.environ["GOOGLE_SA_KEY"] = _sa.name
    except Exception:
        pass


_boot_secrets()

import inventory_core as ic
from us_inventory import USInventoryDB

WH_NAME = {"MI WLOK": "Michigan", "CA ZOHO": "California (ZOHO)",
           "CA XXU": "California (XXU)", "Amazon USA": "Amazon"}
WH_ORDER = ["MI WLOK", "CA ZOHO", "CA XXU", "Amazon USA"]


@st.cache_resource(ttl=1800)
def get_dbs():
    return USInventoryDB(), ic.TWInventory()


@st.cache_resource(ttl=1800)
def get_catalog(family):
    return ic.HubSpotCatalog(family)


def resolve_role(password):
    """密碼決定身分（不是 email）。員工密碼→internal、經銷商密碼→dealer、其他→None。"""
    staff = os.environ.get("STAFF_PASSWORD", "")
    dealer = os.environ.get("DEALER_PASSWORD", "")
    if staff and password == staff:
        return "internal"
    if dealer and password == dealer:
        return "dealer"
    return None


def order_whs(whs):
    return [w for w in WH_ORDER if w in whs] + [w for w in whs if w not in WH_ORDER]


# ---------------- render ----------------
def render_grid(data, internal):
    whs = order_whs(data["warehouses"])
    ip_col = "製程中 / In process" if internal else "In process"
    rows = []
    for r in data["rows"]:
        row = {("顏色 / Color" if internal else "Color"): f"{r['finish']} — {r['color']}"}
        for w in whs:
            row[WH_NAME.get(w, w)] = r["us_by_wh"].get(w, 0)
        row[ip_col] = (str(r["in_process"]) + (" ⚠" if internal and r["tw_low"] else "")) if internal else r["in_process"]
        rows.append(row)
    st.table(rows)
    st.caption(("各倉數字 = 馬上可出的成品組數；製程中 = 可再生產的組數，交期約 1–2 週。"
                if internal else
                "Warehouse numbers = finished sets ready to ship now. "
                "In process = additional sets we can produce, lead time approx. 1–2 weeks."))
    if internal:
        st.markdown("**料號明細 / Component detail（台灣零件庫存）**")
        for r in data["rows"]:
            comps = r.get("tw_components") or []
            mark = " ⚠" if r["tw_low"] else ""
            with st.expander(f"{r['finish']} — {r['color']}  ·  可組 {r['in_process']} 組"
                             f"（瓶頸 {r.get('bottleneck') or '-'}）{mark}", expanded=True):
                st.table([{"零件/Part": c["code"], "料號/ERP": c["erp"] or "-", "每組需/Need": c["need"],
                           "台灣庫存/TW stock": c["qty"], "安全/Safety": c["safety"],
                           "⚠": "低 / Low" if c["below_safety"] else ""}
                          for c in comps])
                loose = r.get("us_loose") or []
                if loose:
                    st.caption("美國零散零件 / US loose: " +
                               "；".join(f"{c['code']} " +
                                        ", ".join(f"{WH_NAME.get(w['warehouse'], w['warehouse'])} {w['qty']}"
                                                  for w in c["stock"]) for c in loose))


def render_single(r, internal):
    s = r["set"]
    st.markdown(f"**{s.get('name', r['model'])}**" + (f"  ·  SKU `{s.get('sku','-')}`" if internal else ""))
    us = r["us"]
    whs = order_whs([x["warehouse"] for x in us["set_stock"] if x["qty"] > 0])
    title = "美國各倉 / United States" if internal else "United States — ready to ship"
    st.markdown(f"**{title}**")
    if whs:
        st.table([{("倉庫 / Warehouse" if internal else "Warehouse"): WH_NAME.get(w, w),
                   ("可出組數 / Sets" if internal else "Sets"): next(x["qty"] for x in us["set_stock"] if x["warehouse"] == w)}
                  for w in whs])
    else:
        st.write("—  (none in stock)" if not internal else "—  目前無現成成品")
    st.write(("美國合計 / US total: **{}** sets".format(us["set_total"])) if internal
             else "US total: **{}** sets".format(us["set_total"]))

    ip = r["tw"]["assemblable_sets"]
    if internal:
        low = r["tw"]["low_stock"]
        st.markdown("**製程中 / In process**  ·  可再組 **{}** 組（交期約 1–2 週）".format(ip))
        st.table([{"零件/Part": c["code"], "料號/ERP": c["erp"] or "-", "每組需/Need": c["need"],
                   "庫存/Stock": c["qty"], "安全/Safety": c["safety"],
                   "⚠": "低 / Low" if c["below_safety"] else ""}
                  for c in r["tw"]["components"]])
        if low:
            st.caption("低於安全存量 / Below safety stock: " + ", ".join(low))
        with st.expander("拆解 / Bill of Materials"):
            st.table([{"零件/Part": d["code"], "數量/Qty": d["count"], "ERP 料號": d["erp"] or "-"}
                      for d in r["decomposition"]])
    else:
        st.markdown("**In process**  ·  {} more sets available, lead time approx. **1–2 weeks**.".format(ip))


# ---------------- app ----------------
st.set_page_config(page_title="Waterson Inventory", layout="centered")

for k in ("email", "role"):
    if k not in st.session_state:
        st.session_state[k] = ""
_qp = st.query_params
# 網址帶 email+pw 可直接進（截圖/分享用）；身分一律由密碼決定
if _qp.get("pw") and not st.session_state.role:
    _r = resolve_role(_qp.get("pw"))
    if _r:
        st.session_state.role = _r
        st.session_state.email = _qp.get("email", "")

if not st.session_state.role:
    st.subheader("Waterson Inventory Lookup")
    st.caption("Enter your email and password to continue. / 輸入 email 與密碼以繼續。")
    email = st.text_input("Email", placeholder="you@watersonusa.com")
    pw = st.text_input("Password / 密碼", type="password")
    if st.button("Continue / 進入", type="primary"):
        role = resolve_role(pw)
        if not email or "@" not in email:
            st.error("Please enter a valid email. / 請輸入有效 email。")
        elif not role:
            st.error("Incorrect password. / 密碼錯誤。")
        else:
            st.session_state.email = email
            st.session_state.role = role
            st.rerun()
    st.stop()

email = st.session_state.email
internal = st.session_state.role == "internal"

top1, top2 = st.columns([4, 1])
top1.caption(f"{email}  ·  {'Internal (Waterson)' if internal else 'Distributor'}")
if top2.button("Sign out / 登出" if internal else "Sign out"):
    st.session_state.email = ""
    st.session_state.role = ""
    st.rerun()

st.subheader("Inventory Lookup" + (" / 庫存查詢" if internal else ""))
model = st.text_input("Model / 型號" if internal else "Model",
                      value=_qp.get("model", ""), placeholder="K51M-400-A3-US32D")
any_color = st.checkbox("Any color — show all finishes" + ("（任何顏色，列出所有顏色）" if internal else ""),
                        value=bool(_qp.get("any")))
go = st.button("Search / 查詢" if internal else "Search", type="primary") or bool(_qp.get("model"))


def is_base_only(m):
    return len(m.strip().upper().split("-")) <= 3


if go and model.strip():
    m = model.strip().upper()
    show_colors = any_color or is_base_only(m)
    base = "-".join(m.split("-")[:3]) if show_colors else m
    try:
        us_db, tw_db = get_dbs()
        cat = get_catalog(ic.family_of(m))
        with st.spinner("Looking up…"):
            if show_colors:
                data = ic.lookup_all_colors(base, catalog=cat, us_db=us_db, tw_db=tw_db)
                if not data["found"]:
                    st.error(f"No variants found for {base}")
                else:
                    st.markdown(f"### {base} — " + ("任何顏色 / any color" if internal else "all finishes"))
                    render_grid(data, internal)
            else:
                r = ic.lookup(m, catalog=cat, us_db=us_db, tw_db=tw_db)
                if not r["found"]:
                    st.error(("找不到型號 / " if internal else "") + f"Model not found: {m}")
                else:
                    render_single(r, internal)
    except Exception as e:
        st.error(f"Error: {type(e).__name__}: {e}")
