#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pre_llm_call 钩子：让记忆自己浮上来，不用 agent 去搜。

收到 Hermes 的 hook payload（stdin JSON），取用户这句话加上最近两轮，
交给 WalnutLocal 算「这段话让你想起哪几条」，把结果作为 context 注入。

三条约束：
  1. 只真想起了东西才说话；没想起就一声不出（输出空）。
  2. 同一会话里同一条记忆不重复念叨（人想起过一次就够了）。
  3. 任何异常都静默退出——钩子不能拖死主循环。
"""
import hashlib
import importlib.util
import json
import os
import sys

HOME = os.path.expanduser("~")
SERVER = os.environ.get("WALNUT_SERVER") or os.path.join(HOME, "WalnutLocal", "server.py")
STATE = os.path.join(HOME, ".hermes", "cache")
MIN_TEXT = 8


def load_server():
    spec = importlib.util.spec_from_file_location("wl_server", SERVER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def seen_path(session_id):
    h = hashlib.sha1((session_id or "nosession").encode()).hexdigest()[:12]
    return os.path.join(STATE, "walnut_recall_%s.json" % h)


def load_seen(p):
    try:
        with open(p, encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_seen(p, ids):
    try:
        os.makedirs(STATE, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(sorted(ids)[-200:], f, ensure_ascii=False)
    except Exception:
        pass


def gather_text(payload):
    extra = payload.get("extra") or {}
    parts = []
    um = extra.get("user_message")
    if isinstance(um, str):
        parts.append(um)
    elif isinstance(um, dict):
        parts.append(um.get("content") or "")
    hist = extra.get("conversation_history")
    if isinstance(hist, list):
        for m in hist[-3:]:
            if isinstance(m, dict):
                c = m.get("content")
                if isinstance(c, str):
                    parts.append(c[:400])
    return " ".join(p for p in parts if p).strip()


def main():
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        return 0
    if os.environ.get("WL_HOOK_DEBUG"):
        try:
            open("/tmp/walnut_hook_payload.json", "w", encoding="utf-8").write(raw[:4000])
        except Exception:
            pass
    text = gather_text(payload)
    if len(text) < MIN_TEXT:
        return 0
    try:
        srv = load_server()
        out = srv.op_context({"text": text, "top_k": 3})
    except Exception:
        return 0
    hits = out.get("triggered") or []
    if not hits:
        return 0

    sp = seen_path(payload.get("session_id"))
    seen = load_seen(sp)
    fresh = [h for h in hits if h["id"] not in seen]
    if not fresh:
        return 0

    lines = ["[记忆] 这段话让你想起这几条（先说，再决定要不要用）："]
    for h in fresh:
        badge = h.get("status") or ""
        if h.get("due"):
            badge += "｜到期 %s" % h["due"]
        if h.get("outcome"):
            badge += "｜已结 %s" % h["outcome"]
        lines.append("- %s（%s）" % (h.get("claim"), badge))
        if h.get("src") and not str(h["src"]).startswith("(none"):
            lines.append("  出处：%s" % h["src"])
        else:
            lines.append("  出处：没有出处，别当成已核实")
        if h.get("resolve_by"):
            lines.append("  什么算看错：%s" % h["resolve_by"])
    if out.get("others_related"):
        lines.append("（另有 %d 条相关，没浮上来）" % out["others_related"])

    save_seen(sp, seen | set(h["id"] for h in fresh))
    sys.stdout.write(json.dumps({"context": "\n".join(lines)[:900]}, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
