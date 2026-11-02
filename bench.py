#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""索引表到底快不快、准不准——拿 68 条真实判断做基准。

四个系统比同一个问题：
  full       全量塞进上下文（理论上限，代价是 token）
  keyword    旧接口 memory_recall 的等价物：关键词全部命中才算（旧接口的失败模式就在这）
  attention  现在在用的：全库逐条打分（O(N)）
  index      新的三级索引：实体哈希收敛候选 -> 只对候选精排

指标：命中率 / MRR / 无关问题误触发率 / 注入字数 / 延迟

测试集是手标的。正例 34 条，其中 10 条故意不说原词（测实体别名和关系边）；
负例 10 条，其中「泸州老窖」是专门用来测「沪电」会不会被单字带出来的。
"""

import json
import os
import statistics
import sys
import time
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from index import JudgmentIndex  # noqa: E402


def _load_server():
    """把 server.py 当模块加载，拿它现在在用的 score / op_context。"""
    p = os.path.join(HERE, "server.py")
    spec = importlib.util.spec_from_file_location("wl_server", p)
    m = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(m)
    except Exception as e:
        print("server.py 加载失败:", e)
        return None
    return m


# (问题, 可接受答案的 id 后缀, 类型)   类型: verbatim / paraphrase / graph / en
CASES = [
    ("沪电股份这个季度毛利率为什么涨，能不能撑住", ["1552d2"], "verbatim"),
    ("沪电那条判断什么时候算我看错", ["1552d2"], "paraphrase"),
    ("中际旭创的三季报出了吗", ["9be0b9"], "verbatim"),
    ("中际旭创公告最新停在哪一天", ["e43b43"], "paraphrase"),
    ("光模块这三家的三季报到底哪天披露", ["c59947", "400939", "69634f", "133e3e"], "verbatim"),
    ("10月26日跌的到底是光模块还是覆铜板", ["f154df"], "verbatim"),
    ("那天A股大跌是不是三季报引起的", ["133e3e", "616af8"], "paraphrase"),
    ("存储涨价这事，哪几家真的赚到了钱", ["c40774"], "paraphrase"),
    ("佰维的存货跌价准备有没有转回进利润", ["95819a"], "verbatim"),
    ("德明利为什么跌，公司有公告吗", ["7bf359", "dadda4"], "verbatim"),
    ("西部数据卖产能、德明利买颗粒，钱落在谁身上", ["dadda4"], "paraphrase"),
    ("Marvell收购Lumentum是多少钱", ["a9fd01"], "verbatim"),
    ("英伟达那三笔20亿入股", ["23ab06", "ef4dcb"], "paraphrase"),
    ("飞艇气囊现在的瓶颈在哪", ["7c48e3"], "verbatim"),
    ("北方华创那份财报说了什么", ["aafc86"], "paraphrase"),
    ("全钒液流电池这门生意卡在哪", ["40d814"], "verbatim"),
    ("三家云厂商花的钱是不是超过赚的了", ["705667"], "paraphrase"),
    ("Intel代工亏损还会不会继续扩大", ["9deb5e"], "verbatim"),
    ("英特尔单日跌28%是怎么回事", ["dfa5c4"], "verbatim"),
    ("满坤那个20cm是财报给的吗", ["0787a8", "829fe6"], "paraphrase"),
    ("胜宏科技什么时候见顶的", ["45f86d"], "verbatim"),
    ("封装和光模块是一条链上的两步吗", ["967134"], "verbatim"),
    ("源杰的三季报出了吗", ["ff3bc0"], "verbatim"),
    ("鸿海10月营收会不会推翻身东山精密四季度", ["7eccd3"], "verbatim"),
    ("SAP的云转型进展", ["66d078"], "en"),
    ("cRPO的增速怎么看", ["7ead3b"], "verbatim"),
    ("那份招股书最该看哪里", ["a77ed5"], "paraphrase"),
    ("备货是主动还是被动，四季度还要不要计提", ["fdacbb"], "verbatim"),
    ("Lam Research那条说法有一手文件吗", ["4edab1"], "verbatim"),
    ("台湾那边的月营收什么时候公布", ["73f8ab", "104cc5"], "paraphrase"),
    ("封测厂扩产是停还是继续，看存货哪个科目", ["f0982b"], "verbatim"),
    ("毛利率掉5个点是一次性的吗", ["873338", "0c49eb"], "paraphrase"),
    ("存货一个季度增加了多少", ["c46779"], "verbatim"),
    ("自由现金流低于预期多少", ["306015"], "verbatim"),
    ("这轮涨价什么时候才咬到戴尔的成本", ["a3ed92"], "graph"),
]

NEGATIVE = [
    "今天天气怎么样",
    "帮我写个python脚本读csv文件",
    "泸州老窖最近怎么样",
    "明天大盘会涨吗",
    "茅台的三季报",
    "我该买什么股票",
    "把这段话翻译成英文",
    "你是谁",
    "比亚迪的销量怎么样",
    "银行股还能不能拿",
]


def norm_ids(idx):
    out = []
    for r in idx.rows:
        out.append((r["id"], r["id"][-6:]))
    return dict(out)


def kw_recall(text, ix, top_k=3):
    """旧接口等价物：关键词全部命中。关键词取问题里最稀有的 3 个二元组。"""
    from index import _bigrams
    qbg = [b for b in _bigrams(text) if b in ix.post]
    qbg.sort(key=lambda b: -ix.idf.get(b, 0))
    keys = qbg[:3]
    if not keys:
        return []
    hit = None
    for k in keys:
        ids = ix.post[k]
        hit = set(ids) if hit is None else (hit & set(ids))
    return list(hit or [])


def chars_of(rows):
    return sum(len(str(r.get("claim") or "")) + len(str(r.get("src") or ""))
               + len(str(r.get("resolve_by") or "")) + 12 for r in rows)


def run(idx, server):
    sfx = norm_ids(idx)
    full_chars = chars_of(idx.rows)

    systems = {
        "full": lambda q: [r["id"] for r in idx.rows],           # 全在上下文里，理论必中
        "keyword": lambda q: kw_recall(q, idx),
        "attention": None,
        "index": None,
    }
    if server:
        def att(q):
            """旧算法：全库逐条打分。必须绕开 op_context（它现在走索引了，
            否则就成了自己跟自己比）。"""
            scored = []
            for r in idx.rows:
                if r.get("status") == "dropped":
                    continue
                sc, _ = server.score(q, r)
                if sc >= 0.22:
                    scored.append((sc, r["id"]))
            scored.sort(key=lambda x: -x[0])
            return [i for _, i in scored[:3]]
        systems["attention"] = att

    def newq(q):
        d = idx.query(q, top_k=3)
        return [x["id"] for x in d["triggered"]]
    systems["index"] = newq

    res = {}
    for name, fn in systems.items():
        if fn is None:
            continue
        hits1 = hits3 = 0
        mrr = 0.0
        lat = []
        chars = []
        for q, acc, kind in CASES:
            acc_ids = {i for i, s in sfx.items() if s in acc}
            t0 = time.perf_counter()
            got = fn(q)
            lat.append((time.perf_counter() - t0) * 1000)
            rank = None
            for i, gid in enumerate(got[:3], 1):
                if gid in acc_ids:
                    rank = i
                    break
            if rank == 1:
                hits1 += 1
            if rank:
                hits3 += 1
                mrr += 1.0 / rank
            if name in ("attention", "index"):
                chars.append(sum(len(str(idx.by_id[g].get("claim") or ""))
                                 + len(str(idx.by_id[g].get("src") or ""))
                                 + len(str(idx.by_id[g].get("resolve_by") or ""))
                                 for g in got if g in idx.by_id))
            elif name == "full":
                chars.append(full_chars)
            else:
                chars.append(sum(chars_of([idx.by_id[g]]) for g in got if g in idx.by_id))
        neg_trig = 0
        for q in NEGATIVE:
            try:
                if fn(q)[:1]:
                    neg_trig += 1
            except Exception:
                pass
        n = len(CASES)
        res[name] = {
            "hit@1": round(hits1 / n, 3),
            "hit@3": round(hits3 / n, 3),
            "MRR": round(mrr / n, 3),
            "false_trigger": "%d/%d" % (neg_trig, len(NEGATIVE)),
            "chars": int(statistics.mean(chars)) if chars else 0,
            "p50_ms": round(statistics.median(lat), 2),
            "p95_ms": round(sorted(lat)[int(len(lat) * 0.95) - 1], 2),
        }
    return res


def _noise_claim(i, rnd):
    """造跟判断库完全不搭边的干扰条目（建材/农牧/餐饮/纺织）。

    关键：不能复用真实词表，否则会把真实倒排链撑大，
    测出来的是"索引被污染所以变快"——那是假数字。
    """
    dom = [
        ("水泥熟料窑线检修", "建材"), ("生猪出栏均重", "农牧"), ("门店翻台率", "餐饮"),
        ("坯布染整加工费", "纺织"), ("连锁药房坪效", "零售"), ("景区客流转化率", "旅游"),
        ("冷链仓库周转", "物流"), ("白酒渠道库存", "白酒"),
    ]
    w, ind = dom[i % len(dom)]
    return {
        "id": "afm_noise%08d" % i,
        "kind": "judgment",
        "claim": "%s这类%02d号样本的%s，第%d次观测，口径可能不一致" % (ind, i % 97, w, i),
        "why": "", "src": "内部台账 %d" % i, "scope": "",
        "resolve_by": "", "due": "2027-0%d-15" % (1 + i % 9),
        "tags": [ind], "status": "proposed", "outcome": "", "outcome_note": "",
        "created": 1793607489 + i, "updated": 1793607489 + i,
    }


def scaling(n_targets, rounds=34):
    """撑大库，看两件事：延迟跟不跟得上、命中率掉不掉。"""
    import statistics as st
    base = [json.loads(l) for l in open(os.path.join(HERE, "memory", "items.jsonl"), encoding="utf-8")]
    sfx = {r["id"]: r["id"][-6:] for r in base}
    qs = [c[0] for c in CASES]
    acc = {c[0]: set(c[1]) for c in CASES}
    out = []
    for N in n_targets:
        rows = [dict(r) for r in base]
        i = 0
        while len(rows) < N:
            rows.append(_noise_claim(i, i)); i += 1
        ix = JudgmentIndex(rows)
        lat_new = []
        hit = 0
        for q in qs:
            t0 = time.perf_counter()
            d = ix.query(q, top_k=3)
            lat_new.append((time.perf_counter() - t0) * 1000)
            if any(sfx.get(x["id"]) in acc[q] for x in d["triggered"] if x["id"] in sfx):
                hit += 1
        lat_old = []
        for q in qs:
            t0 = time.perf_counter()
            for r in rows:
                ix.score(q, r)
            lat_old.append((time.perf_counter() - t0) * 1000)
        out.append({
            "N": len(rows),
            "hit@3": round(hit / len(qs), 3),
            "old_p50": round(st.median(lat_old), 1),
            "new_p50": round(st.median(lat_new), 1),
            "speedup": round(st.median(lat_old) / max(0.01, st.median(lat_new)), 1),
            "build_ms": round(ix._build_time * 1000),
        })
    return out


if __name__ == "__main__":
    ix = JudgmentIndex()
    print("库：", json.dumps(ix.stats(), ensure_ascii=False))
    server = _load_server()
    print("旧实现加载：", "OK" if server else "失败")
    r = run(ix, server)
    print("\n%-10s %7s %7s %7s %10s %8s %8s" % ("系统", "hit@1", "hit@3", "MRR", "误触发", "注入字", "p50ms"))
    for k, v in r.items():
        print("%-10s %7.3f %7.3f %7.3f %10s %8d %8.1f" % (
            k, v["hit@1"], v["hit@3"], v["MRR"], v["false_trigger"], v["chars"], v["p50_ms"]))
    print("\n规模扩展：")
    for row in scaling([68, 500, 2000, 10000, 50000]):
        print("  N=%-6d 命中@3 %.3f  旧 %8.1fms  新 %6.2fms  快 %6.1f 倍  建表 %6dms" % (
            row["N"], row["hit@3"], row["old_p50"], row["new_p50"], row["speedup"], row["build_ms"]))
