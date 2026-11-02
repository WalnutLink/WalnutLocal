#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AgentFullMemory - a memory MCP server that only stores judgments.

Why "only judgments": general-purpose memory servers already store facts,
chat summaries and preferences. Nobody stores the thing that costs money
when it is wrong - a claim with a stated source, an expiry date, and an
explicit "what would prove this wrong". That is what this server keeps.

Design rules (all three come from failures, not from theory):

1. Nothing is written as "true". Everything enters as `proposed` and must be
   promoted deliberately. A memory that writes itself is a memory that
   records the model's own guesses as facts.
2. Every judgment must carry `src` (where it came from) and `resolve_by`
   (what would prove it wrong). Missing source is allowed but flagged
   `unverified` so recall can say "this one has no source".
3. Recall must be able to answer "never recorded" without inventing
   anything. No match returns `no_match`, never a plausible guess.

Transport: MCP stdio (newline-delimited JSON-RPC 2.0), zero dependencies.
Also usable as a plain CLI: --audit / --list / --selftest.

Storage: a single JSONL file plus a generated markdown view, so the whole
memory is readable, diffable and versionable.
"""
import hashlib
import json
import os
import re
import sys
import time
import difflib

VERSION = "0.1.0"
SERVER_NAME = "WalnutLocal"
PROTOCOL = "2024-11-05"

HOME = os.path.expanduser("~")
DATA_DIR = os.environ.get("WL_MEMORY_DIR") or os.path.join(HOME, "WalnutLocal", "memory")
ITEMS = os.path.join(DATA_DIR, "items.jsonl")
VIEW = os.path.join(DATA_DIR, "MEMORY.md")

MAX_ITEMS = 400
MAX_CHARS = 80000
DUP_RATIO = 0.82
KINDS = ("judgment", "preference", "rule")


# ---------------------------------------------------------------- storage

def _now():
    return int(time.time())


def _ensure():
    os.makedirs(DATA_DIR, exist_ok=True)


def load():
    out = []
    if not os.path.exists(ITEMS):
        return out
    with open(ITEMS, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def save_all(rows):
    _ensure()
    tmp = ITEMS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, ITEMS)
    render_view(rows)


def _norm(s):
    s = (s or "").lower()
    return re.sub(r"[\s\u3000,.;:!?()\[\]{}<>\"'`~\-_/\\|+*=#%$^&@]+", "", s)


def _id(claim):
    h = hashlib.sha256((_norm(claim) + str(_now())).encode()).hexdigest()
    return "afm_" + h[:10]


def _resolve_date(s):
    """Accept 2026-12-31 / 20261231 / 2026-12-31T00:00 . Return YYYY-MM-DD."""
    s = (s or "").strip()
    m = re.match(r"^(\d{4})-?(\d{2})-?(\d{2})", s)
    if not m:
        return ""
    return "%s-%s-%s" % m.groups()


# ---------------------------------------------------------------- render

def render_view(rows):
    _ensure()
    lines = ["# AgentFullMemory", "",
             "> Generated from `items.jsonl`. Edit the JSONL, not this file.",
             "> %d records, generated %s" % (len(rows), time.strftime("%Y-%m-%d %H:%M")),
             ""]
    buckets = [("active", "生效中的判断"), ("proposed", "待确认"),
               ("settled", "已结账"), ("dropped", "已作废")]
    for st, title in buckets:
        sel = [r for r in rows if r.get("status") == st]
        if not sel:
            continue
        lines.append("## %s (%d)" % (title, len(sel)))
        lines.append("")
        for r in sel:
            flag = "" if r.get("src") else " **[无出处]**"
            lines.append("- `%s` %s%s" % (r["id"], r.get("claim", ""), flag))
            meta = []
            if r.get("scope"):
                meta.append("范围 %s" % r["scope"])
            if r.get("src"):
                meta.append("出处 %s" % r["src"])
            if r.get("due"):
                meta.append("到期 %s" % r["due"])
            if r.get("resolve_by"):
                meta.append("算错的条件 %s" % r["resolve_by"])
            if r.get("outcome"):
                meta.append("结果 %s" % r["outcome"])
            if meta:
                lines.append("  - " + " ｜ ".join(meta))
        lines.append("")
    with open(VIEW, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------- core ops

def op_propose(a):
    claim = (a.get("claim") or "").strip()
    if not claim:
        return {"ok": False, "error": "claim is required"}
    if len(claim) > 400:
        return {"ok": False, "error": "claim too long (max 400)"}
    kind = (a.get("kind") or "judgment").strip()
    if kind not in KINDS:
        return {"ok": False, "error": "kind must be one of %s" % (KINDS,)}
    rows = load()
    n = _norm(claim)
    for r in rows:
        if r.get("status") in ("dropped", "settled"):
            continue
        if difflib.SequenceMatcher(None, n, _norm(r.get("claim"))).ratio() >= DUP_RATIO:
            return {"ok": False, "duplicate_of": r["id"],
                    "error": "already recorded as %s: %s" % (r["id"], r.get("claim")),
                    "hint": "use memory_ledger to update the existing one instead"}
    if len(rows) >= MAX_ITEMS:
        return {"ok": False, "error": "capacity reached (%d). audit and drop stale ones." % MAX_ITEMS}
    rec = {
        "id": _id(claim),
        "kind": kind,
        "claim": claim,
        "why": (a.get("why") or "").strip(),
        "src": (a.get("src") or "").strip(),
        "scope": (a.get("scope") or "").strip(),
        "resolve_by": (a.get("resolve_by") or "").strip(),
        "due": _resolve_date(a.get("due")),
        "tags": [t.strip() for t in re.split(r"[,，]", a.get("tags") or "") if t.strip()],
        "status": "proposed",
        "outcome": "",
        "outcome_note": "",
        "created": _now(),
        "updated": _now(),
    }
    rows.append(rec)
    save_all(rows)
    warn = []
    if not rec["src"]:
        warn.append("no src: this will be flagged [无出处] on every recall")
    if kind == "judgment" and not rec["resolve_by"]:
        warn.append("judgment without resolve_by: it can never be settled")
    if kind == "judgment" and not rec["due"]:
        warn.append("judgment without due date: it will never surface as overdue")
    return {"ok": True, "id": rec["id"], "status": "proposed",
            "warnings": warn,
            "next": "promote with memory_ledger(id=%s, status='active')" % rec["id"]}


def _match(r, words):
    hay = _norm(" ".join([r.get("claim", ""), r.get("why", ""), r.get("scope", ""),
                          " ".join(r.get("tags") or [])]))
    return all(w in hay for w in words)


# ---------------------------------------------------------------- attention

def _bigrams(s):
    s = _norm(s)
    if len(s) < 2:
        return set([s]) if s else set()
    return set(s[i:i + 2] for i in range(len(s) - 1))


def _scope_keys(r):
    """scope 里的实体名和代码，用来做「这句话提到它了」的判断。"""
    out = set()
    for tok in re.split(r"[\s,，、/|]+", r.get("scope") or ""):
        tok = tok.strip()
        if len(tok) >= 2:
            out.add(tok)
    for m in re.finditer(r"\b(\d{6})\b", (r.get("scope") or "") + " " + (r.get("claim") or "")):
        out.add(m.group(1))
    return out


def score(text, r, now=None):
    """一条记录被这段话「触发」的强度。0 = 完全没想起。

    不是关键词匹配，是注意力：三件事决定它会不会浮上来。
      1) 说的是同一件事（字面重叠）—— 权重最高，但不要求全部命中
      2) 提到了它管的东西（公司名、代码）
      3) 时间上还新鲜、状态上还活着、快到期了（人更容易想起欠着的账）
    """
    now = now or _now()
    t_bg = _bigrams(text)
    if not t_bg:
        return 0.0, ""
    r_text = " ".join([r.get("claim", ""), r.get("why", ""), " ".join(r.get("tags") or [])])
    r_bg = _bigrams(r_text)
    if not r_bg:
        return 0.0, ""
    overlap = len(t_bg & r_bg)
    base = min(1.0, overlap / max(4.0, min(len(r_bg), 24) * 0.35))
    why = "字面重叠 %d" % overlap if overlap else ""

    ent = 0.0
    for k in _scope_keys(r):
        if not k:
            continue
        if k in text:
            ent = 0.55
            why = (why + "｜" if why else "") + "提到 %s" % k
            break
        # 简称也算：说「沪电」的时候，人确实会想起「沪电股份」那条。
        if len(k) >= 3 and k[:2] in text:
            ent = max(ent, 0.55 * 0.7)
            why = (why + "｜" if why else "") + "提到 %s（简称）" % k[:2]
            break

    s = 0.62 * base + 0.38 * ent

    age_days = max(0.0, (now - r.get("updated", now)) / 86400.0)
    fresh = max(0.0, 1.0 - age_days / 180.0)
    s *= (0.75 + 0.25 * fresh)

    st = r.get("status")
    if st == "active":
        s *= 1.12
    elif st == "settled":
        s *= 1.04
    elif st == "dropped":
        s *= 0.2

    due = r.get("due") or ""
    if st == "active" and due and due < time.strftime("%Y-%m-%d"):
        s *= 1.15                       # 欠着的账更容易被想起
        why = (why + "｜" if why else "") + "已过期"
    return round(min(s, 1.0), 3), why


_IDX = {"key": None, "ix": None}


def _get_index():
    """按 items.jsonl 的 mtime 缓存索引。写新判断后下一次调用自动重建。"""
    import importlib.util
    path = os.path.join(DATA_DIR, "items.jsonl")
    try:
        key = (os.path.getmtime(path), os.path.getsize(path))
    except OSError:
        return None
    if _IDX["key"] == key and _IDX["ix"] is not None:
        return _IDX["ix"]
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location("wl_index", os.path.join(here, "index.py"))
    m = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(m)
        ix = m.JudgmentIndex()
    except Exception:
        return None
    _IDX["key"], _IDX["ix"] = key, ix
    return ix


def op_context(a):
    """像人一样：给一段话，返回它让你想起的那几条。不是搜索，是触发。

    返回 0-3 条最相关的，其余只报数量。这是「聚焦」——真想起一件事的时候，
    人不会同时想起八件，是一两件先浮上来，剩下的知道在那儿。
    """
    text = (a.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "text is required"}
    top_k = min(int(a.get("top_k") or 3), 5)
    floor = float(a.get("floor") or 0.22)
    # 走索引表：先按专名/稀有词把候选收敛到 60 条以内，再精排。
    # 库越大这一步越值钱（50,000 条时 1.3ms，逐条扫要 1.6 秒）。
    ix = _get_index()
    if ix is not None:
        d = ix.query(text, top_k=top_k, floor=floor)
        if not d.get("triggered"):
            return {"ok": True, "triggered": [], "others_related": 0,
                    "message": "这套记忆里没有跟这段话相关的东西。别硬扯。"}
        out = []
        for x in d["triggered"]:
            out.append({"id": x["id"], "claim": x["claim"],
                        "src": x["src"] or "(none - unverified)",
                        "resolve_by": x.get("resolve_by") or "",
                        "due": x.get("due") or "", "status": x.get("status"),
                        "outcome": "", "score": x["score"],
                        "triggered_by": x.get("why") or ""})
        note = ("先说这几条，再决定要不要用。src 为空就是没出处，别当成已核实。"
                "如果这条跟现在的事实相反，说清楚哪个对、为什么改。")
        if d.get("ambiguous"):
            note += "另外：前两条分数几乎一样，说明这句话指的不止一件，别挑一条当定论。"
        return {"ok": True, "triggered": out,
                "others_related": d.get("others_related", 0),
                "candidates": d.get("candidates", 0),
                "ambiguous": d.get("ambiguous", False),
                "ms": d.get("ms"), "note": note}

    # 索引坏了就退回逐条扫，绝不因为索引挂了就"想不起来"
    rows = load()
    scored = []
    for r in rows:
        if r.get("status") == "dropped":
            continue
        s, why = score(text, r)
        if s >= floor:
            scored.append((s, r, why))
    scored.sort(key=lambda x: -x[0])
    if not scored:
        return {"ok": True, "triggered": [], "others_related": 0,
                "message": "这套记忆里没有跟这段话相关的东西。别硬扯。"}
    head = scored[:top_k]
    out = []
    for s, r, why in head:
        out.append({"id": r["id"], "claim": r.get("claim"),
                    "src": r.get("src") or "(none - unverified)",
                    "resolve_by": r.get("resolve_by") or "",
                    "due": r.get("due") or "", "status": r.get("status"),
                    "outcome": r.get("outcome") or "",
                    "score": s, "triggered_by": why})
    return {"ok": True, "triggered": out,
            "others_related": max(0, len(scored) - len(head)),
            "note": ("先说这几条，再决定要不要用。src 为空就是没出处，别当成已核实。"
                     "如果这条跟现在的事实相反，说清楚哪个对、为什么改。")}


def op_recall(a):
    q = (a.get("query") or "").strip()
    status = (a.get("status") or "").strip()
    limit = min(int(a.get("limit") or 8), 30)
    rows = load()
    if status:
        rows = [r for r in rows if r.get("status") == status]
    if q:
        words = [_norm(w) for w in re.split(r"[\s,，、;；]+", q) if _norm(w)]
        hits = [r for r in rows if _match(r, words)]
    else:
        hits = rows
    hits.sort(key=lambda r: r.get("updated", 0), reverse=True)
    hits = hits[:limit]
    if not hits:
        return {"ok": True, "no_match": True, "count": 0,
                "message": "nothing recorded on this. Do not infer; say it was never recorded.",
                "query": q}
    out = []
    for r in hits:
        out.append({
            "id": r["id"], "claim": r.get("claim"), "status": r.get("status"),
            "src": r.get("src") or "(none - unverified)",
            "resolve_by": r.get("resolve_by") or "",
            "due": r.get("due") or "",
            "outcome": r.get("outcome") or "",
            "why": r.get("why") or "",
        })
    return {"ok": True, "count": len(out), "items": out,
            "note": "report src verbatim; if src is empty say the record has no source"}


def _dup_groups(rows):
    live = [r for r in rows if r.get("status") not in ("dropped",)]
    pairs = []
    for i in range(len(live)):
        for j in range(i + 1, len(live)):
            a, b = _norm(live[i].get("claim")), _norm(live[j].get("claim"))
            if not a or not b:
                continue
            r = difflib.SequenceMatcher(None, a, b).ratio()
            if r >= DUP_RATIO:
                pairs.append((round(r, 3), live[i]["id"], live[j]["id"]))
    pairs.sort(reverse=True)
    return pairs[:20]


def op_audit(a):
    rows = load()
    today = time.strftime("%Y-%m-%d")
    no_src = [r["id"] for r in rows if not r.get("src") and r.get("status") != "dropped"]
    no_resolve = [r["id"] for r in rows
                  if r.get("kind") == "judgment" and not r.get("resolve_by")
                  and r.get("status") != "dropped"]
    overdue = [{"id": r["id"], "due": r.get("due"), "claim": (r.get("claim") or "")[:70]}
               for r in rows
               if r.get("status") == "active" and r.get("due") and r["due"] < today]
    stale_proposed = [r["id"] for r in rows
                      if r.get("status") == "proposed" and _now() - r.get("created", 0) > 14 * 86400]
    chars = sum(len(r.get("claim", "")) + len(r.get("why", "")) for r in rows)
    rep = {
        "ok": True,
        "records": len(rows),
        "by_status": {},
        "capacity": {"items": "%d/%d" % (len(rows), MAX_ITEMS),
                     "chars": "%d/%d" % (chars, MAX_CHARS)},
        "no_src": no_src,
        "judgment_without_resolve_by": no_resolve,
        "overdue_active": overdue,
        "proposed_over_14d": stale_proposed,
        "duplicates": _dup_groups(rows),
    }
    for r in rows:
        k = r.get("status") or "?"
        rep["by_status"][k] = rep["by_status"].get(k, 0) + 1
    problems = (len(no_src) + len(no_resolve) + len(overdue) + len(stale_proposed)
                + len(rep["duplicates"]))
    rep["problems"] = problems
    rep["verdict"] = "clean" if problems == 0 else "needs attention"
    return rep


def op_ledger(a):
    rid = (a.get("id") or "").strip()
    rows = load()
    if not rid:
        active = [{"id": r["id"], "status": r.get("status"), "due": r.get("due") or "",
                   "claim": (r.get("claim") or "")[:80]}
                  for r in rows if r.get("status") in ("active", "proposed")]
        active.sort(key=lambda x: x.get("due") or "9999")
        return {"ok": True, "open": len(active), "items": active[:30]}
    hit = None
    for r in rows:
        if r["id"] == rid:
            hit = r
            break
    if not hit:
        return {"ok": False, "error": "no record %s" % rid}
    status = (a.get("status") or "").strip()
    outcome = (a.get("outcome") or "").strip()
    if status:
        if status not in ("proposed", "active", "settled", "dropped"):
            return {"ok": False, "error": "bad status"}
        hit["status"] = status
    if outcome:
        if outcome not in ("hit", "miss", "error"):
            return {"ok": False, "error": "outcome must be hit / miss / error"}
        hit["outcome"] = outcome
        hit["status"] = "settled"
    if a.get("outcome_note"):
        hit["outcome_note"] = (a.get("outcome_note") or "").strip()[:600]
    if a.get("resolve_by"):
        hit["resolve_by"] = (a.get("resolve_by") or "").strip()
    if a.get("due"):
        hit["due"] = _resolve_date(a.get("due"))
    hit["updated"] = _now()
    save_all(rows)
    return {"ok": True, "id": rid, "status": hit["status"], "outcome": hit.get("outcome") or "",
            "claim": hit.get("claim")}


# ---------------------------------------------------------------- MCP schema

TOOLS = [
    {
        "name": "memory_propose",
        "description": ("Record a judgment (or a user preference / a standing rule) as PROPOSED. "
                        "Nothing enters memory as true. A judgment should carry src (where the "
                        "claim came from, e.g. a filing and a page) and resolve_by (what would "
                        "prove it wrong) and due (when to check). Returns the record id and "
                        "warnings for whatever is missing."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "claim": {"type": "string", "description": "one sentence, the thing being claimed"},
                "kind": {"type": "string", "enum": list(KINDS),
                         "description": "judgment = a claim that can be proven wrong; preference = how the user wants things done; rule = a standing instruction"},
                "why": {"type": "string", "description": "the reasoning, why this is believed"},
                "src": {"type": "string", "description": "source: file/report + date + page or section"},
                "scope": {"type": "string", "description": "what this applies to: ticker, sector, or task"},
                "resolve_by": {"type": "string", "description": "what observation would prove this wrong"},
                "due": {"type": "string", "description": "YYYY-MM-DD when to check it"},
                "tags": {"type": "string", "description": "comma separated"},
            },
            "required": ["claim"],
        },
    },
    {
        "name": "memory_context",
        "description": ("Give it a passage of conversation. It returns the 0-3 records that "
                        "passage brings to mind, with what triggered each one. Use this instead "
                        "of guessing search terms - you do not need keywords, just the text. "
                        "Returns nothing when nothing is relevant; do not force a connection."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "the passage: user message, or a few recent turns"},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 5},
                "floor": {"type": "number", "description": "minimum strength 0-1, default 0.22"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "memory_recall",
        "description": ("Search memory. Returns records with their source and settlement status. "
                        "If nothing matches it returns no_match and says the thing was never "
                        "recorded - never guess, never fill the gap with a plausible answer."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "words to match (all must appear)"},
                "status": {"type": "string", "enum": ["proposed", "active", "settled", "dropped"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 30},
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory_audit",
        "description": ("Health check: duplicates, records with no source, judgments that can "
                        "never be settled (no resolve_by), active judgments past their due date, "
                        "proposals older than 14 days, and capacity."),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "memory_ledger",
        "description": ("The ledger half. With an id: promote / drop / settle a record. "
                        "outcome=hit|miss|error settles it. Without an id: list what is still "
                        "open, soonest due first."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "status": {"type": "string", "enum": ["proposed", "active", "settled", "dropped"]},
                "outcome": {"type": "string", "enum": ["hit", "miss", "error"]},
                "outcome_note": {"type": "string"},
                "resolve_by": {"type": "string"},
                "due": {"type": "string"},
            },
            "required": [],
        },
    },
]

OPS = {"memory_propose": op_propose, "memory_recall": op_recall, "memory_context": op_context,
       "memory_audit": op_audit, "memory_ledger": op_ledger}


# ---------------------------------------------------------------- transport

def _send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _result(mid, result):
    _send({"jsonrpc": "2.0", "id": mid, "result": result})


def _error(mid, code, msg):
    _send({"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": msg}})


def serve():
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        mid = msg.get("id")
        method = msg.get("method")
        if method == "initialize":
            _result(mid, {
                "protocolVersion": PROTOCOL,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": VERSION},
                "instructions": ("This memory holds judgments, not facts. Every record has a "
                                 "source and a settlement status. When a recall returns "
                                 "no_match, say it was never recorded."),
            })
        elif method in ("notifications/initialized", "initialized"):
            continue
        elif method == "tools/list":
            _result(mid, {"tools": TOOLS})
        elif method == "tools/call":
            p = msg.get("params") or {}
            name = p.get("name")
            args = p.get("arguments") or {}
            fn = OPS.get(name)
            if not fn:
                _error(mid, -32602, "unknown tool %s" % name)
                continue
            try:
                out = fn(args)
            except Exception as e:
                _result(mid, {"content": [{"type": "text", "text": "tool error: %s" % e}],
                              "isError": True})
                continue
            _result(mid, {"content": [{"type": "text",
                                       "text": json.dumps(out, ensure_ascii=False, indent=1)}],
                          "isError": False})
        elif method == "ping":
            _result(mid, {})
        elif method in ("resources/list", "prompts/list"):
            _result(mid, {"resources": []} if method == "resources/list" else {"prompts": []})
        elif mid is not None:
            _error(mid, -32601, "method not found: %s" % method)


# ---------------------------------------------------------------- cli

def main(argv):
    if len(argv) > 1 and argv[1] == "--audit":
        print(json.dumps(op_audit({}), ensure_ascii=False, indent=1))
        return 0
    if len(argv) > 1 and argv[1] == "--list":
        print(json.dumps(op_ledger({}), ensure_ascii=False, indent=1))
        return 0
    if len(argv) > 1 and argv[1] == "--context":
        text = " ".join(argv[2:]) if len(argv) > 2 else sys.stdin.read()
        return _render_context(text)
    if len(argv) > 1 and argv[1] == "--selftest":
        return selftest()
    serve()
    return 0


def _render_context(text):
    """给 hook 用的紧凑输出：只有真想起了东西才说话，最多 3 条，总共不超过 600 字。"""
    out = op_context({"text": text, "top_k": 3})
    hits = out.get("triggered") or []
    if not hits:
        return 0
    lines = ["[记忆] 这段话让你想起这几条（先说，再决定要不要用）："]
    for h in hits:
        badge = h["status"]
        if h["due"]:
            badge += "｜到期 %s" % h["due"]
        if h["outcome"]:
            badge += "｜已结 %s" % h["outcome"]
        lines.append("- %s（%s）" % (h["claim"], badge))
        if h["src"] and not h["src"].startswith("(none"):
            lines.append("  出处：%s" % h["src"])
        else:
            lines.append("  出处：没有出处，别当成已核实")
        if h["resolve_by"]:
            lines.append("  什么算看错：%s" % h["resolve_by"])
    if out.get("others_related"):
        lines.append("（另有 %d 条相关，没浮上来）" % out["others_related"])
    txt = "\n".join(lines)
    sys.stdout.write(json.dumps({"context": txt[:900]}, ensure_ascii=False) + "\n")
    return 0


def selftest():
    import tempfile
    global DATA_DIR, ITEMS, VIEW
    d = tempfile.mkdtemp(prefix="afm_")
    DATA_DIR = d
    ITEMS = os.path.join(d, "items.jsonl")
    VIEW = os.path.join(d, "MEMORY.md")
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name + (" :: " + detail if detail else ""))
        if not cond:
            ok = False

    r1 = op_propose({"claim": "测试判断：某公司三季度毛利率环比下降 1.6 个点",
                     "kind": "judgment", "src": "三季报 P42", "scope": "测试",
                     "resolve_by": "四季报毛利率回升", "due": "2026-12-31"})
    check("propose returns id", r1.get("ok") and r1["id"].startswith("afm_"), str(r1))
    rid = r1["id"]
    r2 = op_propose({"claim": "测试判断：某公司三季度毛利率环比下降1.6个点"})
    check("duplicate rejected", r2.get("ok") is False and r2.get("duplicate_of") == rid, str(r2)[:120])
    r3 = op_recall({"query": "毛利率"})
    check("recall finds it with src", r3["count"] == 1 and r3["items"][0]["src"] == "三季报 P42",
          str(r3)[:160])
    r4 = op_recall({"query": "完全不存在的东西xyz"})
    check("no_match is explicit", r4.get("no_match") is True and "never recorded" in r4["message"])
    r5 = op_ledger({"id": rid, "status": "active"})
    check("promote to active", r5.get("status") == "active", str(r5)[:120])
    r6 = op_audit({})
    check("audit sees active record", r6["records"] == 1 and r6["by_status"].get("active") == 1,
          str(r6["by_status"]))
    r7 = op_propose({"claim": "没有出处的判断", "kind": "judgment"})
    check("missing src warned", any("no src" in w for w in r7.get("warnings", [])), str(r7))
    r8 = op_ledger({"id": rid, "outcome": "miss", "outcome_note": "四季报继续下降"})
    check("settle as miss", r8.get("outcome") == "miss" and r8.get("status") == "settled", str(r8))
    r9 = op_ledger({})
    check("ledger lists open only", r9["open"] == 1, str(r9)[:120])
    check("markdown view written", os.path.exists(VIEW))
    print("\nselftest", "OK" if ok else "FAILED", "| data dir", d)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
