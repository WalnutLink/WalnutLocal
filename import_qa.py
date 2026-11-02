#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把站上已发布的答案回填成本地判断。

为什么回填：站上那 96 条答案本身就是「公开说过的话」。它们现在没有被本地记着，
所以自审看不到它们——而自审最该问的一句话是：
「这些我说出去的话里，有多少条从来没写清什么算看错？」

回填规则（刻意保守，不加工内容）：
  claim      取「主要观点总结」第一句；没有就取正文第一句。超过 120 字截断。
  src        站上原文链接 + 发布日期（可点开核对，这是唯一硬出处）
  scope      标题里能认出的六位代码，或括号里的公司名；认不出就空着
  resolve_by 留空 —— 这些东西压根没写过什么算看错，这正是要暴露的问题
  due        留空
  status     proposed（它们是公开发言，不是已结账的判断）

不做的事：不改写、不总结、不补 resolve_by。补了就看不出问题在哪。
"""
import json
import os
import re
import sqlite3
import sys

HOME = os.path.expanduser("~")
QA = os.path.join(HOME, "a2a-community", "qa.db")
OUT = os.path.join(HOME, "WalnutLocal", "memory", "items.jsonl")
MAX_CLAIM = 120


def pick_conclusion(ans):
    ans = ans or ""
    m = re.split(r"##\s*主要观点总结", ans, 1)
    body = m[1] if len(m) > 1 else ans
    body = re.sub(r"^[#>*\-\s]+", "", body.strip())
    for para in re.split(r"\n\s*\n", body):
        para = re.sub(r"\s+", "", para)
        if len(para) >= 20:
            sent = re.split(r"[。！？]", para)[0]
            return sent[:MAX_CLAIM]
    return ""



def pick_resolve_how(ans):
    """返回 (文本, 来源)。来源=explicit 表示答案里有明确小标题，heuristic 表示按句式猜的。"""
    txt = pick_resolve(ans)
    if not txt:
        return "", ""
    a = ans or ""
    explicit = any(h in a for h in ("什么算看错", "算什么看错", "什么情况算看错"))
    return txt, ("explicit" if txt in re.sub(r"\s+", "", a[a.find("看错"):a.find("看错")+900] if "看错" in a else "") or explicit else "heuristic")


def pick_resolve(ans):
    """答案里若真写了「什么算看错」，就抽出来。没有就是没有，不许编。"""
    a = ans or ""
    for head in ("什么算看错", "算什么看错", "什么情况算看错"):
        i = a.find(head)
        if i < 0:
            continue
        seg = a[i + len(head): i + len(head) + 700]
        seg = seg.lstrip("：: \n*#-—")
        seg = re.sub(r"\s+", "", seg)
        if not seg:
            continue
        parts = re.split(r"[。！？]", seg)
        out = ""
        for pt in parts:
            if len(pt) < 6:
                continue
            out = (out + "。" + pt) if out else pt
            if len(out) >= 40:
                break
        if out:
            return out[:220]
    # 没有小标题的：必须是「如果/若/一旦 … 就错」这种条件句，否则不算。
    # 2026-11-02 实测：只按「就错」找，会把正文里的「算术上就不成立」当成证伪条件。
    plain = re.sub(r"\s+", "", a)
    for sent in re.split(r"[。！？]", plain):
        if len(sent) < 14 or len(sent) > 260:
            continue
        if not any(w in sent for w in ("如果", "若", "一旦", "除非")):
            continue
        if any(w in sent for w in ("就错了", "这条就错", "被证伪", "就站不住", "就不成立")):
            return sent[:220]
    return ""


def pick_scope(title):
    t = title or ""
    m = re.search(r"\b(\d{6})\b", t)
    if m:
        return m.group(1)
    m = re.search(r"（([^）]{2,8})）|\(([^)]{2,8})\)", t)
    if m:
        return (m.group(1) or m.group(2) or "").strip()
    return ""


def main():
    c = sqlite3.connect("file:%s?mode=ro" % QA, uri=True)
    c.row_factory = sqlite3.Row
    rows = c.execute(
        "SELECT id, title, question, answer, answer_ts FROM qa "
        "WHERE status='answered' AND kind='deep' AND COALESCE(hold,0)=0 "
        "AND answer IS NOT NULL AND length(answer)>=800 ORDER BY answer_ts").fetchall()
    c.close()

    existing = []
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        existing.append(json.loads(line))
                    except Exception:
                        pass
    have = set(r.get("from") for r in existing if r.get("from"))
    import hashlib
    import time

    added = 0
    for r in rows:
        key = "qa:%s" % r["id"]
        if key in have:
            for rec in existing:
                if rec.get("from") == key and not rec.get("resolve_by"):
                    got = pick_resolve(r["answer"])
                    if got:
                        rec["resolve_by"] = got
                        rec["resolve_by_how"] = "explicit" if any(h in (r["answer"] or "") for h in ("什么算看错", "算什么看错", "什么情况算看错")) else "heuristic"
            continue
        claim = pick_conclusion(r["answer"])
        if len(claim) < 12:
            continue
        day = time.strftime("%Y-%m-%d", time.localtime(r["answer_ts"] or 0))
        rec = {
            "id": "afm_" + hashlib.sha256(key.encode()).hexdigest()[:10],
            "kind": "judgment",
            "claim": claim,
            "why": "",
            "src": "https://walnutlink.ai/p/%s（%s 回答）" % (r["id"], day),
            "scope": pick_scope(r["title"]),
            "resolve_by": pick_resolve(r["answer"]),
            "resolve_by_how": "explicit" if any(h in (r["answer"] or "") for h in ("什么算看错", "算什么看错", "什么情况算看错")) else ("heuristic" if pick_resolve(r["answer"]) else ""),
            "due": "",
            "tags": ["公开答案"],
            "status": "proposed",
            "outcome": "",
            "outcome_note": "",
            "from": key,
            "created": int(r["answer_ts"] or 0),
            "updated": int(r["answer_ts"] or 0),
        }
        existing.append(rec)
        added += 1

    with open(OUT, "w", encoding="utf-8") as f:
        for rec in existing:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    sys.path.insert(0, os.path.join(HOME, "WalnutLocal"))
    import importlib.util
    spec = importlib.util.spec_from_file_location("srv", os.path.join(HOME, "WalnutLocal", "server.py"))
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    srv.render_view(existing)
    print("候选 %d 条，新增 %d 条，本地共 %d 条" % (len(rows), added, len(existing)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
