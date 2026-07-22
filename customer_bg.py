#!/usr/bin/env python3
"""客戶背景查詢（HubSpot）：給訂單上的客戶 → 回「回頭客/首購、前單、有無 rep、行銷召回」。

給訂單通知用的「客戶背景一行」。純 urllib+certifi，吃環境變數 HUBSPOT_API_TOKEN。
可獨立 CLI 測：python customer_bg.py --name "Greg Robinson" --phone 4254667664
"""
import os
import re
import ssl
import json
import urllib.request

import certifi

HUBSPOT_TOKEN = os.environ.get("HUBSPOT_API_TOKEN", "")
_SSL = ssl.create_default_context(cafile=certifi.where())
_BASE = "https://api.hubapi.com"

# 行銷/自動信主旨關鍵字（判斷是否「行銷召回」而非真人業務往來）
_MARKETING_RE = re.compile(r"reward|new project|projects on the way|\$\d+|coupon|discount|promo", re.I)
# closed-won 但這些 dealstage 代表自助電商單（owner 常為 None）
_ECOMM_STAGE_HINT = ("shipped",)


def _post(path, body):
    req = urllib.request.Request(_BASE + path, data=json.dumps(body).encode(),
                                 headers={"Authorization": "Bearer " + HUBSPOT_TOKEN,
                                          "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, context=_SSL, timeout=30) as r:
        return json.loads(r.read())


def _get(path):
    req = urllib.request.Request(_BASE + path,
                                 headers={"Authorization": "Bearer " + HUBSPOT_TOKEN}, method="GET")
    with urllib.request.urlopen(req, context=_SSL, timeout=30) as r:
        return json.loads(r.read())


def _search(obj, filters, properties, limit=10):
    body = {"filterGroups": filters, "properties": properties, "limit": limit,
            "sorts": [{"propertyName": "createdate", "direction": "DESCENDING"}]}
    return _post(f"/crm/v3/objects/{obj}/search", body).get("results", [])


def _assoc_ids(from_obj, oid, to_obj):
    try:
        r = _get(f"/crm/v4/objects/{from_obj}/{oid}/associations/{to_obj}")
        return [x["toObjectId"] for x in r.get("results", [])]
    except Exception:
        return []


def _norm_phone(p):
    d = re.sub(r"\D", "", p or "")
    return d[1:] if len(d) == 11 and d.startswith("1") else d


def find_contact(name=None, email=None, phone=None):
    """依 email→name→phone 順序找 contact（回第一個命中）。"""
    props = ["firstname", "lastname", "email", "phone", "mobilephone", "city", "state",
             "createdate", "lifecyclestage", "num_associated_deals"]
    # 1) email 精準
    if email:
        r = _search("contacts", [{"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}], props)
        if r:
            return r[0]
    # 2) 姓名
    if name and name.strip():
        parts = name.strip().split()
        if len(parts) >= 2:
            first, last = parts[0], parts[-1]
            r = _search("contacts", [{"filters": [
                {"propertyName": "firstname", "operator": "EQ", "value": first},
                {"propertyName": "lastname", "operator": "EQ", "value": last}]}], props)
            if r:
                return r[0]
    # 3) 電話（phone / mobilephone 皆比對）
    if phone:
        pn = _norm_phone(phone)
        if pn:
            for field in ("phone", "mobilephone"):
                r = _search("contacts", [{"filters": [
                    {"propertyName": field, "operator": "CONTAINS_TOKEN", "value": pn}]}], props)
                if r:
                    return r[0]
    return None


def background(name=None, email=None, phone=None, current_order_no=None):
    """回客戶背景 dict + 一行摘要 `summary`（給通知用）。找不到人 → 首購/新客戶。"""
    out = {"found": False, "is_repeat": False, "prior_orders": [], "has_rep": False,
           "rep_dialog": False, "marketing_nudge": False, "lifecycle": None,
           "contact_id": None, "summary": "新客戶（HubSpot 無紀錄）"}
    try:
        c = find_contact(name, email, phone)
    except Exception as e:
        out["summary"] = f"客戶背景查詢失敗（{e.__class__.__name__}）"
        return out
    if not c:
        return out  # 查無 = 全新
    out["found"] = True
    cid = c["id"]
    p = c["properties"]
    out["contact_id"] = cid
    out["lifecycle"] = p.get("lifecyclestage")

    # 關聯 deals → 前單 & 有無 rep
    deal_ids = _assoc_ids("contacts", cid, "deals")
    prior = []
    has_rep = False
    if deal_ids:
        r = _post("/crm/v3/objects/deals/batch/read",
                  {"properties": ["dealname", "dealstage", "amount", "hubspot_owner_id", "createdate"],
                   "inputs": [{"id": str(d)} for d in deal_ids]})
        for d in r.get("results", []):
            dp = d["properties"]
            dn = dp.get("dealname") or ""
            if dp.get("hubspot_owner_id"):
                has_rep = True
            # 排除本張單（用訂單號比對）
            if current_order_no and current_order_no.lstrip("#") in dn:
                continue
            prior.append({"name": dn, "amount": dp.get("amount"), "created": dp.get("createdate"),
                          "owner": dp.get("hubspot_owner_id")})
    prior.sort(key=lambda x: x["created"] or "")
    out["prior_orders"] = prior
    out["is_repeat"] = len(prior) > 0
    out["has_rep"] = has_rep

    # 往來活動：有真人業務對話？行銷召回？
    email_ids = _assoc_ids("contacts", cid, "emails")
    if email_ids:
        r = _post("/crm/v3/objects/emails/batch/read",
                  {"properties": ["hs_email_subject", "hs_email_direction", "hs_timestamp"],
                   "inputs": [{"id": str(e)} for e in email_ids[:25]]})
        for e in r.get("results", []):
            ep = e["properties"]
            subj = ep.get("hs_email_subject") or ""
            direction = ep.get("hs_email_direction") or ""
            if direction == "INCOMING_EMAIL":
                out["rep_dialog"] = True  # 客戶回過信 = 有真人往來
            if _MARKETING_RE.search(subj):
                out["marketing_nudge"] = True
    # 真人往來也看 calls / meetings
    if _assoc_ids("contacts", cid, "calls") or _assoc_ids("contacts", cid, "meetings"):
        out["rep_dialog"] = True

    out["summary"] = _summarize(out)
    return out


def _prior_no(name):
    m = re.search(r"#(\d+)", name or "")
    return "#" + m.group(1) if m else None


def _summarize(bg):
    bits = []
    if bg["is_repeat"]:
        n = len(bg["prior_orders"]) + 1
        first_no = _prior_no(bg["prior_orders"][0]["name"]) if bg["prior_orders"] else None
        s = f"回頭客・第 {n} 單"
        if first_no:
            s += f"（前單 {first_no}）"
        bits.append(s)
    else:
        bits.append("首購" if bg["found"] else "新客戶（HubSpot 無紀錄）")
    if bg["rep_dialog"]:
        bits.append("有業務往來")
    elif bg["has_rep"]:
        bits.append("有 owner")
    else:
        bits.append("無 rep（純電商）")
    if bg["marketing_nudge"]:
        bits.append("行銷召回")
    return "・".join(bits)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--name")
    ap.add_argument("--email")
    ap.add_argument("--phone")
    ap.add_argument("--order")
    a = ap.parse_args()
    print(json.dumps(background(a.name, a.email, a.phone, a.order), ensure_ascii=False, indent=2))
