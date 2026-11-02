#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Walnut Local - 本地自审账本。

用途只有一件：让 agent 自己查自己。不对外、不发布、不参与任何外部记录。

为什么不复制一份判断：memory/items.jsonl 是判断的唯一权威位置。
这个库只存 memory 里没有的东西——「我什么时候审过它、发现了什么、怎么处置的」。
两处存同一件事，就一定会有一处腐化（2026-11-01 记忆治理踩过的坑）。

四类自审（就是记忆腐化的四种形态）：

  1. 到期没结    active 且 due 已过
  2. 永远结不了  judgment 没有 resolve_by（等于免责声明，说了等于没说）
  3. 没出处      src 为空
  4. 自相矛盾    同一 scope 里两条方向相反的活跃判断

第 4 类是唯一需要动脑子的，用启发式：同 scope + 文本相似 + 方向词相反。
启发式会误报，所以每条都能被人工标记「已处置」，处置过的下次不再报。

用法：
    python3 ledger.py audit           # 体检，列出该审的
    python3 ledger.py review <id>     # 标记某条已审 / 已处置
    python3 ledger.py log             # 看处置历史
"""
import json
import os
import re
import sqlite3
import sys
import time
import difflib

HOME = os.path.expanduser("~")
BASE = os.environ.get("WL_BASE") or os.path.join(HOME, "WalnutLocal")
DATA = os.path.join(BASE, "memory")
ITEMS = os.path.join(DATA, "items.jsonl")
DB = os.path.join(BASE, "ledger.db")

SIM_MIN = 0.55          # 同 scope 里算「讲同一件事」的相似度下限

# 方向词：左边和右边互为反义。命中不同侧才算矛盾。
DIRECTIONS = [
    ("上升", "下降"), ("增加", "减少"), ("扩大", "收窄"), ("改善", "恶化"),
    ("高于", "低于"), ("提升", "下滑"), ("走高", "走低"), ("增长", "萎缩"),
    ("提前", "推迟"), ("提升", "降低"), ("超预期", "不及预期"),
]


def _now():
    return int(time.time())


def _norm(s):
    return re.sub(r"[\s\u3000,.;:!?()\[\]{}<>\"'`~\-_/\\|+*=#%$^&@]+", "", (s or "").lower())


def load_items():
    out = []
    if not os.path.exists(ITEMS):
        return out
    with open(ITEMS, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    return out


def conn():
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        target TEXT NOT NULL,        -- 记录 id，或 pair:<a>|<b>
        issue TEXT NOT NULL,         -- 哪一类问题
        verdict TEXT NOT NULL,       -- confirmed / dismissed / fixed
        note TEXT,
        ts INTEGER NOT NULL)""")
    c.commit()
    return c


def reviewed(c):
    """已经处置过的 (target, issue) 组合，下次不再报。"""
    rows = c.execute("SELECT target, issue FROM reviews").fetchall()
    return set((r[0], r[1]) for r in rows)


def _direction_conflict(a, b):
    """两条判断是否方向相反。返回命中的那对词，或 None。"""
    na, nb = _norm(a), _norm(b)
    for pos, neg in DIRECTIONS:
        pa, pb = _norm(pos), _norm(neg)
        if (pa in na and pb in nb) or (pb in na and pa in nb):
            return (pos, neg)
    return None


def audit(show_all=False):
    items = load_items()
    c = conn()
    seen = reviewed(c)
    today = time.strftime("%Y-%m-%d")
    issues = []

    for r in items:
        if r.get("status") == "dropped":
            continue
        if r.get("status") == "active" and r.get("due") and r["due"] < today:
            issues.append({"issue": "overdue", "target": r["id"],
                           "claim": r.get("claim", ""),
                           "detail": "到期 %s 还没结账" % r["due"]})
        if r.get("kind") == "judgment" and not r.get("resolve_by"):
            issues.append({"issue": "no_resolve", "target": r["id"],
                           "claim": r.get("claim", ""),
                           "detail": "没写「什么算看错」，这条永远结不了账"})
        if not r.get("src"):
            issues.append({"issue": "no_src", "target": r["id"],
                           "claim": r.get("claim", ""),
                           "detail": "没有出处，以后没法核对"})

    live = [r for r in items if r.get("status") in ("active", "proposed")]
    seen_pairs = set()
    for i in range(len(live)):
        for j in range(i + 1, len(live)):
            a, b = live[i], live[j]
            if (a.get("scope") or "") != (b.get("scope") or ""):
                continue
            if not a.get("scope"):
                continue
            sim = difflib.SequenceMatcher(None, _norm(a.get("claim")),
                                          _norm(b.get("claim"))).ratio()
            if sim < SIM_MIN:
                continue
            pair = _direction_conflict(a.get("claim"), b.get("claim"))
            if not pair:
                continue
            key = "pair:%s|%s" % tuple(sorted([a["id"], b["id"]]))
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            issues.append({"issue": "contradiction", "target": key,
                           "claim": "%s  ×  %s" % (a.get("claim", ""), b.get("claim", "")),
                           "detail": "同一范围（%s）两条方向相反：%s / %s，相似度 %.2f"
                                     % (a.get("scope"), pair[0], pair[1], sim)})

    fresh = [x for x in issues if (x["target"], x["issue"]) not in seen]
    handled = [x for x in issues if (x["target"], x["issue"]) in seen]
    c.close()
    return {"total_items": len(items), "found": len(issues),
            "new": len(fresh), "handled_before": len(handled),
            "items": (issues if show_all else fresh)}


def do_review(target, issue, verdict, note):
    c = conn()
    c.execute("INSERT INTO reviews(target, issue, verdict, note, ts) VALUES (?,?,?,?,?)",
              (target, issue, verdict, note or "", _now()))
    c.commit()
    c.close()
    return {"ok": True, "target": target, "issue": issue, "verdict": verdict}


def do_log(limit=20):
    c = conn()
    rows = c.execute("SELECT target, issue, verdict, note, ts FROM reviews "
                     "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    c.close()
    return [{"target": r[0], "issue": r[1], "verdict": r[2], "note": r[3],
             "when": time.strftime("%Y-%m-%d %H:%M", time.localtime(r[4]))} for r in rows]


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "audit"
    if cmd == "audit":
        out = audit(show_all="--all" in argv)
        if not out["items"]:
            print("自审干净：%d 条记忆，没有新问题。" % out["total_items"])
            if out["handled_before"]:
                print("（另有 %d 条以前处置过，不再报）" % out["handled_before"])
            return 0
        print("自审发现 %d 个新问题（共 %d 条记忆）：\n" % (out["new"], out["total_items"]))
        for x in out["items"]:
            print("[%s] %s" % (x["issue"], x["target"]))
            print("   %s" % x["detail"])
            print("   %s" % (x["claim"] or "")[:110])
        print("\n处置方式：python3 ledger.py review <target> <issue> "
              "confirmed|dismissed|fixed \"说明\"")
        return 1
    if cmd == "review":
        if len(argv) < 5:
            print("用法: ledger.py review <target> <issue> confirmed|dismissed|fixed [说明]")
            return 2
        note = argv[5] if len(argv) > 5 else ""
        print(json.dumps(do_review(argv[2], argv[3], argv[4], note), ensure_ascii=False))
        return 0
    if cmd == "log":
        for r in do_log():
            print("%s  %-12s %-9s %s  %s" % (r["when"], r["issue"], r["verdict"], r["target"][:22], r["note"]))
        return 0
    print("用法: ledger.py [audit|review|log]")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
