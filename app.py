#!/usr/bin/env python3
"""Waterson Inventory Lookup — Streamlit 前端（簡潔、工作取向）。設計邏輯見 ARCHITECTURE.md。

身分（由密碼決定，email 只做紀錄）：
  - STAFF_PASSWORD  → 內部：中英並列，完整三地（料號、可組數、卡在哪）。
  - DEALER_PASSWORD → 經銷商：純英文。只給數字；不露台灣/倉別/料號，交期寫 ~1–2 weeks。

查詢：
  - 打到顏色（K51M-450-C4-US19）→ 單色。
  - 只打基礎型號（K51M-450-C4）或勾「Any color」→ 每色一列，美國可出最多的在前。
  - 批次：貼整段文字，自動抓最多 10 個型號。
內部單查與任何顏色共用同一套版面：摘要表（美國現成｜美國可出｜台灣可組｜卡在）→ 美國怎麼出 → 台灣明細（收合）。
美國出不了貨時列其他有貨顏色；-316 沒貨時同色 304 排第一。
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

WH_NAME = {"MI WLOK": "Michigan", "CA ZOHO": "California (ZOHO)", "NV ZOHO": "Nevada (ZOHO)"}
WH_ORDER = ["MI WLOK", "CA ZOHO", "NV ZOHO"]


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
def _tw_comp_row(c):
    """台灣單片庫存表的一列；缺料號（無法查庫存）的單片明確標「無料號」而非假裝 0。
    整欄一律 str，避免 st.table 混 int/str 觸發 Arrow 警告。"""
    unv = c.get("unverifiable")
    return {"零件/Part": c["code"],
            "料號/ERP": ("❌ 無料號 / no ERP" if unv else (c["erp"] or "-")),
            "每組需/Need": str(c["need"]),
            "台灣庫存/TW stock": ("查不到 / N/A" if unv else str(c["qty"])),
            "安全/Safety": ("-" if unv else str(c["safety"])),
            "⚠": ("缺料號 / missing" if unv else ("低 / Low" if c["below_safety"] else ""))}


# ---------- 共用版面：單一顏色（內部）與 any-color 用同一套 ----------
# view = 從 ic.lookup 結果或 lookup_all_colors 的列整理出來的統一格式，讓兩種頁面長得一樣。
def _view_from_lookup(r):
    ship, how = ic.us_ship(r)
    tw = r["tw"]
    return {"model": r["model"], "us_total": r["us"]["set_total"],
            "us_ship": ship, "us_ship_how": how, "us_set_stock": r["us"]["set_stock"],
            "us_decomposed": r["us"].get("decomposed"), "us_loose": r["us"].get("components") or [],
            "decomposition": r["decomposition"], "in_process": tw["assemblable_sets"],
            "tw_unconfirmable": bool(tw.get("unconfirmable")), "missing_erp": tw.get("missing_erp") or [],
            "bottleneck": tw.get("bottleneck"), "tw_sets_1ca": tw.get("sets_1ca", 0),
            "tw_sub_parts": tw.get("sub_parts") or [], "tw_components": tw.get("components") or []}


def _bottleneck_label(v):
    """台灣卡在哪（單片 or 半成品；只有一片在用的半成品叫「SA 半成品」）。"""
    if v.get("tw_unconfirmable"):
        return "缺料號 / no ERP: " + "、".join(v.get("missing_erp") or [])
    b = v.get("bottleneck")
    if not b:
        return "-"
    if not any(x["erp"] == b for x in v.get("tw_sub_parts") or []):
        c = next((x for x in v.get("tw_components") or [] if x["code"] == b), None)
        return f"{b} 單片（剩 {c['qty']}）" if c else b
    s = next((x for x in v["tw_sub_parts"] if x["erp"] == b), None)
    if not s:
        return b
    users = [u["code"] for u in s.get("used_by") or []]
    name = f"{users[0]} 半成品" if len(users) == 1 else (s.get("desc") or b)
    return f"{name}（剩 {s['qty']}）"


def _us_cell(v, internal=True):
    """美國可出那格：數字＋怎麼出（拆組直接寫 C3 套組＋DS 散片）。經銷商只給數字。"""
    n = v.get("us_ship", 0)
    how = v.get("us_ship_how")
    if not internal or how in (None, "ready"):
        return str(n)
    if how == "split":
        d = v["us_decomposed"]
        ex = "＋".join(e["code"] for e in d["extra_pieces"])
        return f"{n}（{d['via']['code']} 套組＋{ex} 散片）"
    return f"{n}（散片湊 / loose）"


def _set_code(v):
    """套組碼（K51M-450-C4-US19 → C4；K51GG-SA-US32D → SA）。"""
    return ic.set_code_of(v.get("model") or "") or "套組"


def _summary_cells(v):
    code = _set_code(v)
    return {f"美國現成 {code} / US ready {code}": str(v.get("us_total", 0)),
            "美國可出 / US can ship": _us_cell(v),
            "台灣可組 / TW": ("無法確認 / N/A" if v.get("tw_unconfirmable") else str(v["in_process"])),
            "卡在 / Bottleneck": _bottleneck_label(v)}


_SUMMARY_NOTE = ("美國可出 = 美國現在能出的組數（現成套組、散片湊組、拆組取最多的那種）；台灣可組 = 只需包裝即可出貨"
                 "（少量可隔天，量多約 3 天）。/ US = sets we can ship from US now (ready, loose or split-fill); "
                 "TW = sets we assemble, packing only (next day ~ 3 days).")


def _render_us_how(v):
    """美國怎麼出：現成套組→各倉；拆組→子集套組＋散片各在哪倉；散片湊→各片在哪倉；都不行→缺哪片。"""
    how = v.get("us_ship_how")
    if how == "ready":
        st.table([{"美國怎麼出 / US source": "現成套組 / ready set", "倉庫 / Warehouse": WH_NAME.get(x["warehouse"], x["warehouse"]),
                   "組數 / Sets": str(x["qty"])} for x in v["us_set_stock"] if x["qty"] > 0])
        return
    if how == "split":
        d = v["us_decomposed"]
        st.caption(f"{_set_code(v)} 完整套組美國 0 組，改用現成 {d['via']['code']} 套組＋散片在美國湊成整組 / "
                   f"no complete {_set_code(v)} set in US — combine a ready {d['via']['code']} set with loose pieces.")
        rows = [{"美國怎麼出 / US source": f"現成 {d['via']['code']} 套組 / ready set", "每組需 / Per set": "1",
                 "倉庫 / Warehouse": _wh_hint(d["via"]["stock"])}]
        rows += [{"美國怎麼出 / US source": f"{e['code']} 散片 / loose", "每組需 / Per set": str(e["per_set"]),
                  "倉庫 / Warehouse": _wh_hint(e["stock"])} for e in d["extra_pieces"]]
        st.table(rows)
        return
    loose = {c["code"]: c for c in v.get("us_loose") or []}
    if how == "loose":
        st.caption(f"{_set_code(v)} 完整套組美國 0 組，全用散片湊 / no complete {_set_code(v)} set in US — all from loose pieces.")
        st.table([{"美國怎麼出 / US source": f"{d['code']} 散片 / loose", "每組需 / Per set": str(d["count"]),
                   "倉庫 / Warehouse": _wh_hint(loose[d["code"]]["stock"])} for d in v["decomposition"]])
        return
    short = [d["code"] for d in v["decomposition"]
             if (loose[d["code"]]["total"] if d["code"] in loose else 0) < d["count"]]
    have = "、".join(f"{c} 散片 {loose[c]['total']}" for c in loose)
    st.caption(f"美國無現成 {_set_code(v)} 套組，散片也湊不出整組" + (f"，缺 {'、'.join(short)}" if short else "")
               + (f"（有 {have}）" if have else "") + " / Not available in US"
               + (f" — short {', '.join(short)}" if short else "") + ".")


def _render_tw_detail(v):
    """台灣明細：無法確認的說明＋成品單片表＋（有的話）半成品表。"""
    if v.get("tw_unconfirmable"):
        m = "、".join(v.get("missing_erp") or [])
        st.warning(f"單片 {m} 未建料號，台灣庫存查不到，所以可組數無法確認。/ "
                   f"Piece(s) {m} have no ERP, so TW stock cannot be confirmed.")
    st.table([_tw_comp_row(c) for c in v.get("tw_components") or []])
    subs = v.get("tw_sub_parts") or []
    if subs:
        st.caption("半成品 / Sub-parts (2CA)：成品單片不夠時用這些組裝 / used when finished pieces are short")
        st.table([{"半成品/Sub-part": s.get("desc") or "-", "料號/ERP": s["erp"],
                   "每組需/Need per set": str(s["per_set"]), "庫存/Stock": str(s["qty"]),
                   "安全/Safety": str(s["safety"])} for s in subs])


def _render_single_internal(r):
    v = _view_from_lookup(r)
    st.table([_summary_cells(v)])
    st.caption(_SUMMARY_NOTE)
    _render_us_how(v)
    with st.expander("台灣明細 / TW detail", expanded=False):
        _render_tw_detail(v)


def render_grid(data, internal):
    """顏色總表：跟單一顏色同一套欄位；下方每個顏色一個收合區塊，內容同單一顏色的明細。"""
    rows = []
    for r in data["rows"]:
        if internal:
            rows.append({"顏色 / Color": f"{r['finish']} — {r['color']}", **_summary_cells(r)})
        else:
            rows.append({"Color": f"{r['finish']} — {r['color']}", "Ready (US)": _us_cell(r, internal=False),
                         "In process": ("N/A" if r.get("tw_unconfirmable") else str(r["in_process"]))})
    st.table(rows)
    st.caption(_SUMMARY_NOTE if internal else
               "Ready (US) = sets ready to ship now. In process = additional sets we can produce, "
               "lead time approx. 1–2 weeks.")
    if internal:
        for r in data["rows"]:
            with st.expander(f"{r['finish']} — {r['color']}  ·  現成 {r['us_total']}｜美國可出 {_us_cell(r)}｜台灣 "
                             + ("無法確認" if r.get("tw_unconfirmable") else str(r["in_process"])), expanded=False):
                _render_us_how(r)
                st.markdown("**台灣明細 / TW detail**")
                _render_tw_detail(r)


def _wh_hint(stock):
    """[{warehouse,qty}] → 'MI W-Lock 6、CA ZOHO 17' 之類的簡短來源字串。"""
    return "、".join(f"{WH_SHORT.get(x['warehouse'], x['warehouse'])} {x['qty']}" for x in stock)


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
    tw_unconf = bool(r["tw"].get("unconfirmable"))
    missing = r["tw"].get("missing_erp") or []
    tw_display = ("無法確認 / N/A" if tw_unconf else f"{tw_sets} {unit}")
    tw_display_en = ("N/A" if tw_unconf else f"{tw_sets} {unit}")
    c1, c2 = st.columns(2)
    tw_label_zh = "台灣庫存 / In stock (TW)" if acc else "台灣可再組 / In process (隔天~3天 / 1–3 days)"
    tw_label_en = "In stock (TW)" if acc else "In process (~1–2 wk)"
    if internal:
        c1.metric("美國現貨可出 / Ready now", f"{us_total} {unit}")
        c2.metric(tw_label_zh, tw_display)
    else:
        c1.metric("Ready to ship", f"{us_total} {unit}")
        c2.metric(tw_label_en, tw_display_en)
    # 一行結論（醒目）：有現貨→綠、無法確認→黃(說明)、台灣有貨→黃、皆無→紅
    if us_total > 0:
        extra = ("（台灣可組數無法確認・缺料號 / TW: cannot confirm, missing ERP）" if tw_unconf
                 else (f"（台灣另有 {tw_sets} {unit}）" if acc else f"（另可再生產 {tw_sets} {unit}）") if tw_sets else "")
        st.success((f"**現貨可出 {us_total} {unit}，可立即出貨。**" + extra) if internal
                   else f"**{us_total} {unit} ready to ship now.**")
    elif tw_unconf:
        st.warning(("**美國無現貨；台灣可組數無法確認——單片 {m} 未建料號（wh_erp 空白），"
                    "庫存查不到。需先補建料號才能判斷能否組裝出貨。**\n\n"
                    "**US: none in stock. TW: cannot confirm — piece(s) {m} have no ERP (wh_erp is blank), "
                    "so their stock cannot be looked up. The ERP must be created before we can tell whether "
                    "the set can be assembled.**").format(m="、".join(missing)) if internal
                   else "**Made to order — availability to be confirmed. Please contact us for lead time.**")
    elif tw_sets and tw_sets > 0:
        if acc:
            st.warning(f"**美國無現貨；台灣有庫存 {tw_sets} 件（需自台灣調貨，交期約 1–2 週）。**" if internal
                       else f"**{tw_sets} pcs available, lead time approx. 1–2 weeks.**")
        else:
            st.warning((f"**美國無現貨；台灣可再生產 {tw_sets} 組，只需包裝即可出貨（少量可隔天、量多約 3 天）。**\n\n"
                        f"**US: none in stock. TW: {tw_sets} set(s) can be assembled — packing only, "
                        f"small qty ships next day, larger ~3 days.**") if internal
                       else f"**Made to order — {tw_sets} sets, lead time approx. 1–2 weeks.**")
    else:
        st.error("**目前美國與台灣皆無庫存。**" if internal
                 else "**Currently unavailable.**")
    # 美國優先『拆組出貨』：完整套組美國無現貨、但可用「較小現成套組＋散裝單片」在美國湊成整組。
    dec = r["us"].get("decomposed")
    if us_total == 0 and dec and not acc and not shim:
        via = dec["via"]
        parts_txt = "＋".join(e["code"] for e in dec["extra_pieces"])
        if internal:
            src_via = _wh_hint(via["stock"])
            multi = len(dec["extra_pieces"]) > 1
            src_extra = "；".join((f"{e['code']} " if multi else "") + _wh_hint(e["stock"])
                                  for e in dec["extra_pieces"])
            st.info(("**美國可拆組出貨 {n} 組**：用現成 **{v}** 套組（{sv}）＋散裝單片 **{p}**（{se}）"
                     "在美國組成整組，仍從美國出貨、免動台灣（是否採用由業務決定）。\n\n"
                     "**US split-fill: {n} set(s)** — combine ready **{v}** sets ({sv}) with loose **{p}** "
                     "piece(s) ({se}); still ships from US.").format(
                         n=dec["sets"], v=via["code"], sv=src_via, p=parts_txt, se=src_extra))
        else:
            st.info("**{n} set(s) can be fulfilled from US** (assembled from US stock, ships from US).".format(
                n=dec["sets"]))


def render_single(r, internal):
    s = r["set"]
    st.markdown(f"**{s.get('name', r['model'])}**" + (f"  ·  SKU `{s.get('sku','-')}`" if internal else ""))
    if internal and not r["tw"].get("is_accessory") and not r["tw"].get("is_shim"):
        _render_single_internal(r)   # 內部：與 any-color 同一套版面（摘要表→美國怎麼出→台灣明細）
        render_alts(r, internal)
        return
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

    # 美國拆組明細（完整套組無現貨、可用較小套組＋散片湊出時；內部才列料號級明細）
    dec = us.get("decomposed")
    if us["set_total"] == 0 and dec and internal:
        via = dec["via"]
        bn = dec["bottleneck"]
        st.markdown("**美國可拆組出貨 / US split-fill: {} 組 / sets**".format(dec["sets"]))
        st.caption("完整套組美國無現貨，但可用現成較小套組＋散裝單片在美國組成整組出貨（免動台灣）。/ "
                   "No complete set in US, but can be assembled from a smaller ready set + loose pieces, "
                   "still shipping from US.")
        rows = [{"來源 / Source": "現成套組 / ready set", "項目 / Item": via["code"],
                 "每組需 / Per set": "1",
                 "美國庫存 / US stock": f"{_wh_hint(via['stock'])}（共 / total {via['total']}）"}]
        for e in dec["extra_pieces"]:
            rows.append({"來源 / Source": "散裝單片 / loose piece", "項目 / Item": e["code"],
                         "每組需 / Per set": str(e["per_set"]),
                         "美國庫存 / US stock": f"{_wh_hint(e['stock'])}（共 / total {e['total']}）"})
        st.table(rows)
        st.caption("可拆組 {n} 組，瓶頸 {b}。/ {n} sets can be split-filled; bottleneck: {b}.".format(
            n=dec["sets"], b=bn))

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
        subs = tw.get("sub_parts") or []
        if tw.get("unconfirmable"):
            missing = tw.get("missing_erp") or []
            st.markdown("**製程中 / In process**  ·  ⚠️ **無法確認可組數 / cannot confirm**")
            st.warning(("單片 **{m}** 未建料號（wh_erp 空白），台灣庫存查不到。出貨需全部單片齊備，"
                        "故此顏色可組數**無法確認**——請先補建這些單片的料號後再判斷。\n\n"
                        "Piece(s) **{m}** have no ERP (wh_erp is blank), so TW stock can't be looked up. "
                        "All pieces are required to ship, so this quantity **cannot be confirmed** — "
                        "please create the ERP for these pieces first.").format(m="、".join(missing)))
        else:
            s1 = tw.get("sets_1ca")
            s2 = tw.get("sets_from_sub")
            # 有第三層（2CA 半成品）時，把總數拆成「1CA 現成單片配對 + 2CA 半成品補組」
            breakdown = ""
            if subs:
                breakdown = "（現成單片 / ready pieces {} ＋ 半成品組裝 / from sub-parts {}）".format(s1, s2)
            st.markdown("**製程中 / In process**  ·  可再生產 / can produce **{}** sets{}（只需包裝即可出貨：少量可隔天、量多約 3 天 / packing only: next day–3 days）".format(ip, breakdown))
        st.caption("成品單片庫存 / Finished single-piece (1CA) stock")
        st.table([_tw_comp_row(c) for c in r["tw"]["components"]])
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
        if r["tw"].get("unconfirmable"):
            st.markdown("**In process**  ·  Made to order — availability to be confirmed, lead time approx. **1–2 weeks**.")
        else:
            st.markdown("**In process**  ·  {} more sets available, lead time approx. **1–2 weeks**.".format(ip))

    render_alts(r, internal)


def render_alts(r, internal):
    """缺色 / 316 替代建議：美國出不了貨時，列其他有貨顏色（316 先給同色 304）。資料來自 ic.color_alternatives。"""
    alts = r.get("_alts") or []
    if not alts:
        return
    is_316 = r["model"].endswith("-316")
    how_zh = {"loose": "散片湊", "split": "拆組"}
    if internal:
        st.warning("316 美國無貨，其他 316 顏色或改 304 / 316 not available in US — other 316 or 304 options:" if is_316 else
                   "此顏色美國無貨，其他有貨顏色 / Not available in US — other finishes:")
        st.table([{"顏色 / Color": ("304 同色 / same color" if a["same_color_304"]
                                  else f"{a['finish']} — {a['color']}"),
                   "美國可出 / US": str(a["us_ship"]) + (f"（{how_zh[a['us_ship_how']]}）"
                                                        if a["us_ship_how"] in how_zh else ""),
                   "台灣可組 / TW": str(a["tw_sets"]),
                   "型號 / Model": a["model"]} for a in alts])
    else:
        st.warning("Marine grade (316) unavailable — other 316 or 304 options:" if is_316 else
                   "This finish is currently unavailable — other finishes:")
        st.table([{"Color": ("Same color, 304" if a["same_color_304"] else f"{a['finish']} — {a['color']}"),
                   "Ready (US)": str(a["us_ship"]), "In process": str(a["tw_sets"])} for a in alts])


def _batch_status(us_total, tw_sets, internal, tw_unconf=False):
    if us_total > 0:
        return f"現貨 {us_total} 組，可立即出" if internal else f"{us_total} ready to ship"
    if tw_unconf:
        return "無法確認/N/A（缺料號）" if internal else "to be confirmed"
    if tw_sets and tw_sets > 0:
        return f"台灣可組 {tw_sets} 組（隔天~3天/1–3d）" if internal else f"make-to-order {tw_sets} (1–2 wk)"
    return "無貨" if internal else "unavailable"


WH_SHORT = {"MI WLOK": "MI W-Lock", "CA ZOHO": "CA ZOHO", "NV ZOHO": "NV ZOHO"}


def wh_short(w):
    """批次表用的短欄名；Amazon 各種寫法（Amazon USA / Canada…）一律縮成 Amazon。"""
    if "AMAZON" in (w or "").upper():
        return "Amazon"
    return WH_SHORT.get(w, WH_NAME.get(w, w))


def render_batch(results, internal):
    """一次多個型號：頂端 bold 摘要表（業務掃一眼），下面每型號可展開看完整明細。
    美國各倉分欄顯示（不加總掉），讓業務看得出每個位置各有多少現貨。"""
    st.markdown((f"### 查詢結果（{len(results)} 個型號）" if internal else f"### Results ({len(results)})"))
    # ── 收集有貨的美國倉（跨所有型號），依標準順序分欄 ──
    whset = set()
    for m, r in results:
        if r.get("found"):
            for s in r["us"]["set_stock"]:
                if s["qty"] > 0:
                    whset.add(s["warehouse"])
    whs = order_whs(whset)
    wh_cols = [wh_short(w) for w in whs]
    # ── 摘要表（markdown → 數字可加粗）：型號 | 各美國倉… | 美國小計 | 台灣可再組 | 狀態 ──
    if internal:
        cols = ["型號 / Model"] + wh_cols + ["美國小計 / US", "台灣可再組 / In proc.", "狀態 / Status"]
    else:
        cols = ["Model"] + wh_cols + ["US total", "In process", "Status"]
    align = [":--"] + ["--:"] * (len(wh_cols) + 2) + [":--"]
    head = ["| " + " | ".join(cols) + " |", "|" + "|".join(align) + "|"]
    ncell = len(wh_cols) + 2  # 各倉 + 美國小計 + 台灣
    for m, r in results:
        if not r.get("found"):
            head.append("| `" + m + "` | " + " | ".join(["—"] * ncell) + " | "
                        + ("查無此型號" if internal else "not found") + " |")
            continue
        by = {s["warehouse"]: s["qty"] for s in r["us"]["set_stock"]}
        cells = [(f"**{by.get(w, 0)}**" if by.get(w, 0) > 0 else "0") for w in whs]
        us_total = r["us"]["set_total"]
        tw_sets = r["tw"]["assemblable_sets"]
        tw_unconf = bool(r["tw"].get("unconfirmable"))
        tw_cell = "無法確認/N/A" if tw_unconf else f"**{tw_sets}**"
        row = ["`" + m + "`"] + cells + [f"**{us_total}**", tw_cell,
                                         _batch_status(us_total, tw_sets, internal, tw_unconf)]
        head.append("| " + " | ".join(row) + " |")
    st.markdown("\n".join(head))
    st.caption(("各倉數字 = 該倉馬上可出的成品組數（分開顯示，非加總）；美國小計 = 各倉合計；"
                "台灣可再組 = 半成品可再生產，只需包裝即可出貨（少量可隔天，量多約 3 天）。"
                " / In process = assembled from semi-finished parts (packing only — small qty next day, larger ~3 days)."
                if internal else
                "Each warehouse column = finished sets ready at that location. "
                "In process = additional sets, lead time ~1–2 weeks."))
    # ── 各型號明細（展開）──
    st.markdown("---")
    for m, r in results:
        if not r.get("found"):
            with st.expander(f"{m} — {'查無 / not found' if internal else 'not found'}"):
                st.write(r.get("error") or ("找不到型號 / Model not found"))
            continue
        us_total = r["us"]["set_total"]
        tw_sets = r["tw"]["assemblable_sets"]
        if r["tw"].get("unconfirmable"):
            title = (f"{m}　·　美國 {us_total} 組／台灣可組 無法確認/N/A（缺料號）" if internal
                     else f"{m} — ready {us_total} / in process N/A")
        else:
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
    show_colors = any_color or ic.is_base_only(m)
    base = ic.base_of(m) if show_colors else m
    try:
        us_db, tw_db = get_dbs()
        tw_sw_db = get_tw_sw() if ic.is_swing_clear(m) else None
        cat = get_catalog(ic.family_of(m))
        with st.spinner("Looking up…"):
            if show_colors:
                data = ic.lookup_all_colors(base, catalog=cat, us_db=us_db, tw_db=tw_db, tw_sw_db=tw_sw_db,
                                            include_316=ic.wants_316(m))   # -316 → 316 與 304 各色
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
                    if (ic.us_ship(r)[0] == 0 and not r["tw"].get("is_accessory")
                            and not r["tw"].get("is_shim")):
                        try:
                            r["_alts"] = ic.color_alternatives(m, catalog=cat, us_db=us_db,
                                                               tw_db=tw_db, tw_sw_db=tw_sw_db)
                        except Exception:
                            r["_alts"] = []
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
