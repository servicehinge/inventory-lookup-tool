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
    # Hugging Face / 任何只給環境變數的平台：service account 用整段 JSON 字串注入
    if not os.environ.get("GOOGLE_SA_KEY") and os.environ.get("GCP_SERVICE_ACCOUNT_JSON"):
        _sa = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        _sa.write(os.environ["GCP_SERVICE_ACCOUNT_JSON"])
        _sa.flush()
        os.environ["GOOGLE_SA_KEY"] = _sa.name


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
def get_tw_sw():
    return ic.TWSwingClear()


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


def render_headline(r, internal):
    """業務最在意的兩個數字做大：美國現貨 / 台灣（可再組 or 配件庫存）。放在每筆結果最上面。"""
    acc = r["tw"].get("is_accessory")
    shim = r["tw"].get("is_shim")
    if shim:
        unit = "包" if internal else "packs"
    elif acc:
        unit = "件" if internal else "pcs"
    else:
        unit = "組" if internal else "sets"
    us_total = r["us"]["set_total"]
    tw_sets = r["tw"]["assemblable_sets"]
    c1, c2 = st.columns(2)
    tw_label_zh = "台灣庫存 / In stock (TW)" if acc else "台灣可再組 / In process (~1–2 wk)"
    tw_label_en = "In stock (TW)" if acc else "In process (~1–2 wk)"
    if internal:
        c1.metric("美國現貨可出 / Ready now", f"{us_total} {unit}")
        c2.metric(tw_label_zh, f"{tw_sets} {unit}")
    else:
        c1.metric("Ready to ship", f"{us_total} {unit}")
        c2.metric(tw_label_en, f"{tw_sets} {unit}")
    # 一行結論（醒目）：有現貨→綠、台灣有貨→黃、皆無→紅
    if us_total > 0:
        extra = (f"（台灣另有 {tw_sets} {unit}）" if acc else f"（另可再生產 {tw_sets} {unit}）") if tw_sets else ""
        st.success((f"**現貨可出 {us_total} {unit}，可立即出貨。**" + extra) if internal
                   else f"**{us_total} {unit} ready to ship now.**")
    elif tw_sets > 0:
        if acc:
            st.warning(f"**美國無現貨；台灣有庫存 {tw_sets} 件（需自台灣調貨，交期約 1–2 週）。**" if internal
                       else f"**{tw_sets} pcs available, lead time approx. 1–2 weeks.**")
        else:
            st.warning(f"**美國無現貨；台灣可再生產 {tw_sets} 組，交期約 1–2 週。**" if internal
                       else f"**Made to order — {tw_sets} sets, lead time approx. 1–2 weeks.**")
    else:
        st.error("**目前美國與台灣皆無庫存。**" if internal
                 else "**Currently unavailable.**")


def render_single(r, internal):
    s = r["set"]
    st.markdown(f"**{s.get('name', r['model'])}**" + (f"  ·  SKU `{s.get('sku','-')}`" if internal else ""))
    render_headline(r, internal)
    acc = r["tw"].get("is_accessory")
    shim = r["tw"].get("is_shim")
    if shim:
        unit_en = "packs"
        qty_col = ("可出包數 / Packs" if internal else "Packs")
    elif acc:
        unit_en = "pcs"
        qty_col = ("可出件數 / Pcs" if internal else "Pcs")
    else:
        unit_en = "sets"
        qty_col = ("可出組數 / Sets" if internal else "Sets")
    us = r["us"]
    whs = order_whs([x["warehouse"] for x in us["set_stock"] if x["qty"] > 0])
    title = "美國各倉 / United States" if internal else "United States — ready to ship"
    st.markdown(f"**{title}**")
    if whs:
        st.table([{("倉庫 / Warehouse" if internal else "Warehouse"): WH_NAME.get(w, w),
                   qty_col: next(x["qty"] for x in us["set_stock"] if x["warehouse"] == w)}
                  for w in whs])
    else:
        st.write("—  (none in stock)" if not internal else "—  目前無現成成品 / none ready to ship")
    st.write(("美國合計 / US total: **{}** {}".format(us["set_total"], unit_en)) if internal
             else "US total: **{}** {}".format(us["set_total"], unit_en))

    ip = r["tw"]["assemblable_sets"]
    if shim:
        tw = r["tw"]
        c = tw["components"][0]
        if internal:
            low = "  ·  ⚠ 低於安全存量 / below safety" if c["below_safety"] else ""
            st.markdown(f"**台灣庫存 / TW stock**  ·  **{tw['pcs']:,}** 片 / pcs（＝ **{tw['packs']:,}** 包 / packs；"
                        f"料號 / ERP `{c['erp']}`）{low}")
            st.caption(f"美國以「包」計（每包 {tw['pack_size']} 片）；台灣以單片計、已換算成包供比較。"
                       f"安全存量 {tw['safety_pcs']:,} 片。/ US counts packs ({tw['pack_size']} pcs each); "
                       f"TW counts pieces, converted to packs.")
        else:
            st.markdown(f"**In stock**  ·  **{tw['packs']:,}** packs available (lead time ~1–2 weeks if not in US).")
        render_alts(r, internal)
        return
    if r["tw"].get("is_accessory"):
        c = r["tw"]["components"][0]
        if internal:
            note = "  ·  ⚠ 低於安全存量 / below safety" if c["below_safety"] else ""
            st.markdown(f"**台灣庫存 / TW stock**  ·  **{ip}** 件 / pcs（料號 / ERP `{c['erp'] or '-'}`）{note}")
            st.caption("此為配件（門檔）獨立單件，非套組；數量＝可出件數。/ Standalone accessory (door stop); count = pieces.")
        else:
            st.markdown(f"**In stock**  ·  **{ip}** pcs available (lead time approx. 1–2 weeks if not in US).")
        render_alts(r, internal)
        return
    if internal:
        low = r["tw"]["low_stock"]
        tw = r["tw"]
        s1 = tw.get("sets_1ca")
        s2 = tw.get("sets_from_sub")
        subs = tw.get("sub_parts") or []
        # 有第三層（2CA 半成品）時，把總數拆成「1CA 現成單片配對 + 2CA 半成品補組」
        breakdown = ""
        if subs:
            breakdown = "（現成單片 / ready pieces {} ＋ 半成品組裝 / from sub-parts {}）".format(s1, s2)
        st.markdown("**製程中 / In process**  ·  可再生產 / can produce **{}** sets{}（交期約 / lead time ~1–2 weeks）".format(ip, breakdown))
        st.caption("成品單片庫存 / Finished single-piece (1CA) stock")
        st.table([{"零件/Part": c["code"], "料號/ERP": c["erp"] or "-", "每組需/Need": c["need"],
                   "庫存/Stock": c["qty"], "安全/Safety": c["safety"],
                   "⚠": "低 / Low" if c["below_safety"] else ""}
                  for c in r["tw"]["components"]])
        if subs:
            st.caption("半成品 / Sub-parts (2CA) — 單片無現貨時靠這些組裝；共用件（如背板）以整套需求計 / "
                       "used to assemble when finished pieces are short; shared parts counted per full set")
            st.table([{"半成品/Sub-part": s.get("desc") or "-", "料號/ERP": s["erp"],
                       "每組需/Need per set": s["per_set"], "庫存/Stock": s["qty"],
                       "安全/Safety": s["safety"],
                       "⚠": ("低 / Low" if s["below_safety"] else ("無此料 / not in TW" if not s.get("in_table") else ""))}
                      for s in subs])
        if low:
            st.caption("低於安全存量 / Below safety stock: " + ", ".join(low))
        with st.expander("拆解 / Bill of Materials"):
            st.table([{"零件/Part": d["code"], "數量/Qty": d["count"], "ERP 料號": d["erp"] or "-"}
                      for d in r["decomposition"]])
    else:
        st.markdown("**In process**  ·  {} more sets available, lead time approx. **1–2 weeks**.".format(ip))

    render_alts(r, internal)


def render_alts(r, internal):
    """缺色提示：列出其他「有貨」顏色，讓業務能提替代方案。只列出有庫存的顏色；中英並列。"""
    us_alt = r["us"].get("alt_colors") or []
    tw_alt = r["tw"].get("alt_colors") or []
    if not us_alt and not tw_alt:
        return
    out_of_stock = r["us"]["set_total"] == 0 and r["tw"]["assemblable_sets"] == 0
    n = len(set([a["color"] for a in us_alt] + [a["color"] for a in tw_alt]))

    if internal:
        if out_of_stock:
            st.warning(f"此顏色目前無貨；另有 {n} 種顏色有貨可向客戶提案 / "
                       f"This finish is out of stock — {n} other finish(es) available to offer:")
        else:
            st.markdown(f"**其他顏色（{n} 種）/ Other finishes available ({n})**")
        if us_alt:
            st.markdown("　**美國現貨 / Ready to ship (US):**")
            st.table([{"顏色 / Color": f"{a['color_name']} ({a['color']})",
                       "可出組數 / Sets": a["total"],
                       "倉庫 / Warehouse": ", ".join(f"{WH_NAME.get(w, w)}: {q}"
                                                     for w, q in a["by_wh"].items())}
                      for a in us_alt])
        if tw_alt:
            st.markdown("　**製程中 / In process (~1–2 weeks):**")
            st.table([{"顏色 / Color": f"{a['color_name']} ({a['color']})",
                       "可再生產組數 / Sets": a["sets"]} for a in tw_alt])
    else:
        if out_of_stock:
            st.warning(f"This finish is currently unavailable. {n} other finish(es) you can offer:")
        else:
            st.markdown(f"**Other finishes available ({n})**")
        if us_alt:
            st.markdown("**Ready to ship:**")
            st.table([{"Color": f"{a['color_name']} ({a['color']})", "Sets": a["total"]} for a in us_alt])
        if tw_alt:
            st.markdown("**In process (~1–2 weeks):**")
            st.table([{"Color": f"{a['color_name']} ({a['color']})", "Sets": a["sets"]} for a in tw_alt])


def _batch_status(us_total, tw_sets, internal):
    if us_total > 0:
        return f"現貨 {us_total} 組，可立即出" if internal else f"{us_total} ready to ship"
    if tw_sets > 0:
        return f"需生產 {tw_sets} 組（約 1–2 週）" if internal else f"make-to-order {tw_sets} (1–2 wk)"
    return "無貨" if internal else "unavailable"


def render_batch(results, internal):
    """一次多個型號：頂端 bold 摘要表（業務掃一眼），下面每型號可展開看完整明細。"""
    st.markdown((f"### 查詢結果（{len(results)} 個型號）" if internal else f"### Results ({len(results)})"))
    # ── 摘要表（markdown → 數字可加粗）──
    if internal:
        head = ["| 型號 / Model | 美國現貨 / Ready | 台灣可再組 / In process | 狀態 / Status |",
                "|:--|--:|--:|:--|"]
    else:
        head = ["| Model | Ready to ship | In process | Status |",
                "|:--|--:|--:|:--|"]
    for m, r in results:
        if not r.get("found"):
            head.append(f"| `{m}` | — | — | {'查無此型號' if internal else 'not found'} |")
            continue
        us_total = r["us"]["set_total"]
        tw_sets = r["tw"]["assemblable_sets"]
        head.append(f"| `{m}` | **{us_total}** | **{tw_sets}** | {_batch_status(us_total, tw_sets, internal)} |")
    st.markdown("\n".join(head))
    st.caption(("美國現貨 = 馬上可出的成品組數；台灣可再組 = 半成品可再生產，交期約 1–2 週。"
                if internal else
                "Ready to ship = finished sets available now. In process = additional sets, lead time ~1–2 weeks."))
    # ── 各型號明細（展開）──
    st.markdown("---")
    for m, r in results:
        if not r.get("found"):
            with st.expander(f"{m} — {'查無 / not found' if internal else 'not found'}"):
                st.write(r.get("error") or ("找不到型號 / Model not found"))
            continue
        us_total = r["us"]["set_total"]
        tw_sets = r["tw"]["assemblable_sets"]
        title = (f"{m}　·　美國 {us_total} 組／台灣可再組 {tw_sets} 組" if internal
                 else f"{m} — ready {us_total} / in process {tw_sets}")
        with st.expander(title):
            render_single(r, internal)


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


if go and model.strip() and ic.is_shim_query(model):
    # Metal Door Shim：配件包裝品，直接單品查詢（不走 any-color、不需 HubSpot catalog）
    try:
        us_db, tw_db = get_dbs()
        with st.spinner("Looking up…"):
            r = ic.lookup(model, us_db=us_db, tw_db=tw_db)
        if not r["found"]:
            st.error(("找不到 / " if internal else "") + "Metal Door Shim not found")
        else:
            render_single(r, internal)
    except Exception as e:
        st.error(f"Error: {type(e).__name__}: {e}")
elif go and model.strip():
    m = ic.normalize_model(model)  # swing clear 各種寫法（K51L-SWRH / SWLH / 空白）收斂成 K51LSWRH
    show_colors = any_color or is_base_only(m)
    base = "-".join(m.split("-")[:3]) if show_colors else m
    try:
        us_db, tw_db = get_dbs()
        tw_sw_db = get_tw_sw() if ic.is_swing_clear(m) else None
        cat = get_catalog(ic.family_of(m))
        with st.spinner("Looking up…"):
            if show_colors:
                data = ic.lookup_all_colors(base, catalog=cat, us_db=us_db, tw_db=tw_db, tw_sw_db=tw_sw_db)
                if not data["found"]:
                    st.error(f"No variants found for {base}")
                else:
                    st.markdown(f"### {base} — " + ("任何顏色 / any color" if internal else "all finishes"))
                    render_grid(data, internal)
            else:
                r = ic.lookup(m, catalog=cat, us_db=us_db, tw_db=tw_db, tw_sw_db=tw_sw_db)
                if not r["found"]:
                    st.error(("找不到型號 / " if internal else "") + f"Model not found: {m}")
                else:
                    render_single(r, internal)
    except Exception as e:
        st.error(f"Error: {type(e).__name__}: {e}")


# ---------------- 批次查詢：貼上一整段文字，一次查多個型號 ----------------
st.divider()
st.markdown("**批次查詢 / Batch lookup**" if internal else "**Batch lookup**")
st.caption(("貼上訂單或報價明細（整段即可），自動抓出型號一次查最多 10 個。"
            if internal else
            "Paste order or quote lines — models are detected automatically (up to 10)."))
blob = st.text_area("貼上明細 / Paste here" if internal else "Paste here", height=150,
                    placeholder="K51M-450-A3-US19 ...\nK51M-400-B3-US32D ...\nK51P-500-A2-US19 ...")
go_batch = st.button("查詢全部 / Look up all" if internal else "Look up all", type="primary")

if go_batch and blob.strip():
    models = ic.extract_models(blob)
    if not models:
        st.error("沒有抓到任何型號 / No models detected（型號需含連字號，如 K51M-400-A3-US32D）"
                 if internal else "No models detected.")
    else:
        try:
            us_db, tw_db = get_dbs()
            results = []
            with st.spinner(f"查詢 {len(models)} 個型號… / Looking up {len(models)}…"):
                for m in models:
                    try:
                        tw_sw_db = get_tw_sw() if ic.is_swing_clear(m) else None
                        cat = get_catalog(ic.family_of(m))
                        results.append((m, ic.lookup(m, catalog=cat, us_db=us_db, tw_db=tw_db, tw_sw_db=tw_sw_db)))
                    except Exception as e:
                        results.append((m, {"found": False, "error": f"{type(e).__name__}: {e}"}))
            render_batch(results, internal)
        except Exception as e:
            st.error(f"Error: {type(e).__name__}: {e}")
