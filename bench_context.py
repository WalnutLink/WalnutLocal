#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多轮测试：用户这句没头没尾的时候，上文能不能接住。

为什么要单独测：单轮测试集里每条问题都自带公司名和术语，测不出"上下文"的价值。
真实对话里最常见的追问是"那这个季度能不能撑住""那钱落在谁身上"——
单看这句，人也不知道在说谁，必须有上文。

四个方案：
  single     只用用户当前这句
  concat     把上文和当前这句拼成一坨（我第一版的错做法）
  weighted   按段算分再加权，当前这句 1.0，上文 0.4
  weighted7  同上，但上文权重 0.7（看权重有多敏感）
"""

import json
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from index import JudgmentIndex  # noqa: E402

# (我上一轮说的话, 用户这句, 可接受答案的 id 后缀)
DIALOGUES = [
    ("沪电股份三季度毛利率升3.1个点，主要来自AI服务器高多层板占比提升。证伪条件是四季报高多层板占比回落但毛利率仍升。",
     "那它这个季度能不能撑住？", ["1552d2"]),
    ("美光、闪迪、佰维、普冉、澜起这五个名字挂在同一个存储涨价故事下，但涨价的钱只落在两个地方。",
     "那钱到底落在谁身上？", ["c40774"]),
    ("中际旭创的三季报到现在还没发，最新一份公告停在10月23日。",
     "那到底哪天出？", ["9be0b9", "400939", "e43b43"]),
    ("佰维这个季度利润好，主要是存货跌价准备转回，不是卖得多。",
     "那这笔钱进利润表了吗？", ["95819a"]),
    ("满坤科技10月30日涨停20.01%，市场说是因为三季报。",
     "那真的是财报给的吗？", ["0787a8", "829fe6"]),
    ("全钒液流电池这门生意，上半年全国装机规模只有91MW，卡的不在产能。",
     "所以卡的是哪一头？", ["40d814"]),
    ("三家云厂商这一季花掉的钱都超过了当期赚来的现金。",
     "那它们还够花吗？", ["705667"]),
    ("Marvell在谈收购Lumentum，双方都没官宣，但已经有三份文件露出来了。",
     "这笔交易到底多少钱？", ["a9fd01"]),
    ("胜宏科技5月26日就见顶了，比华正新材、南亚新材、生益科技都早。",
     "那是不是板厂先松的？", ["45f86d"]),
    ("源杰科技的三季报还没出，最新一份是8月29日的半年报。",
     "那最新那份说了什么？", ["ff3bc0"]),
    ("判断封测厂扩产是停还是继续，不能看合同负债，要看存货里的两个科目。",
     "具体看哪两个科目？", ["f0982b"]),
    ("Intel代工这块的亏损，剔除一次性计提之后还在扩大。",
     "那还会不会继续？", ["9deb5e"]),
]

# 长上文：真实场景里我上一轮的话常常上千字，关键结论在前面，后面全是细节。
# 这一组是"拼接 vs 分段加权"的决胜局——拼接会被长文淹掉。
FILLER = ("具体到报表层面，还要看合同负债、存货结构、应收账款周转天数这几个科目；"
          "如果只看利润表增速，很容易把一次性收益当成经营改善。另外这家公司在"
          "同一份文件里还改了口径，把原来单列的项目合并进了其他项，这件事本身也要注意。") * 3

LONG_DIALOGUES = [
    ("先说结论：沪电股份三季度毛利率升3.1个点，主因是AI服务器高多层板占比提升，不是涨价。"
     + FILLER + "所以这条判断的证伪条件是：四季报里高多层板占比回落，但毛利率还在升。",
     "那这个季度能不能撑住？", ["1552d2"]),
    ("先说结论：美光、闪迪、佰维、普冉、澜起这五家挂在同一个存储涨价故事下，"
     "但钱只落在两个地方——能自己定价的，和手里压着一堆货的。" + FILLER,
     "那钱到底落在谁身上？", ["c40774"]),
    ("先说结论：Marvell在谈收购Lumentum，约830亿美元、约28倍销售额，双方都还没官宣。"
     + FILLER, "这笔交易到底多少钱？", ["a9fd01"]),
    ("先说结论：三家云厂商这一季花掉的钱都超过了当期赚来的现金。" + FILLER,
     "那它们还够花吗？", ["705667"]),
    ("先说结论：全钒液流电池卡的不在产能，在上半年全国装机只有91MW。" + FILLER,
     "所以卡的是哪一头？", ["40d814"]),
]

# 上文和当前这句都不相干的，应当一声不出
NEG_DIALOGUES = [
    ("我帮你把这段代码重构好了，函数拆成了三个。", "那还有别的问题吗？"),
    ("今天的日程是下午三点开会，在二楼会议室。", "那几点能结束？"),
    ("这个周末我打算去爬山，路线已经查好了。", "那要带什么东西？"),
]


def rank_of(hits, acc, sfx):
    for i, h in enumerate(hits[:3], 1):
        if sfx.get(h["id"]) in acc:
            return i
    return 99


def main():
    ix = JudgmentIndex()
    sfx = {r["id"]: r["id"][-6:] for r in ix.rows}
    systems = {
        "single": lambda prev, cur: ix.query(cur, top_k=3)["triggered"],
        "concat": lambda prev, cur: ix.query("%s %s" % (prev[:400], cur), top_k=3)["triggered"],
        "weighted": lambda prev, cur: ix.query_multi([(cur, 1.0), (prev, 0.4)], top_k=3)["triggered"],
        "weighted7": lambda prev, cur: ix.query_multi([(cur, 1.0), (prev, 0.7)], top_k=3)["triggered"],
    }

    rows = {}
    for name, fn in systems.items():
        h1 = h3 = 0
        lat = []
        wrong = 0        # 浮上来了但浮错了
        empty = 0
        for prev, cur, acc in DIALOGUES:
            import time
            t0 = time.perf_counter()
            hits = fn(prev, cur)
            lat.append((time.perf_counter() - t0) * 1000)
            r = rank_of(hits, set(acc), sfx)
            if r == 1:
                h1 += 1
            if r <= 3:
                h3 += 1
            elif hits:
                wrong += 1
            else:
                empty += 1
        silent = 0
        for prev, cur in NEG_DIALOGUES:
            if fn(prev, cur):
                silent += 1
        rows[name] = {"hit@1": h1 / len(DIALOGUES), "hit@3": h3 / len(DIALOGUES),
                      "浮错": wrong, "没浮": empty,
                      "误触发": "%d/%d" % (silent, len(NEG_DIALOGUES)),
                      "p50ms": statistics.median(lat)}

    print("多轮追问（%d 条正例 + %d 条负例）" % (len(DIALOGUES), len(NEG_DIALOGUES)))
    print("%-10s %7s %7s %5s %5s %10s %8s" % ("方案", "hit@1", "hit@3", "浮错", "没浮", "误触发", "p50ms"))
    for k, v in rows.items():
        print("%-10s %7.3f %7.3f %5d %5d %10s %8.2f" % (
            k, v["hit@1"], v["hit@3"], v["浮错"], v["没浮"], v["误触发"], v["p50ms"]))
    print()
    print("长上文（%d 条，上文 900+ 字）" % len(LONG_DIALOGUES))
    print("%-10s %7s %7s %5s %5s" % ("方案", "hit@1", "hit@3", "浮错", "没浮"))
    for name, fn in systems.items():
        h1 = h3 = wrong = empty = 0
        for prev, cur, acc in LONG_DIALOGUES:
            r = rank_of(fn(prev, cur), set(acc), sfx)
            if r == 1:
                h1 += 1
            if r <= 3:
                h3 += 1
            elif fn(prev, cur):
                wrong += 1
            else:
                empty += 1
        n = len(LONG_DIALOGUES)
        print("%-10s %7.3f %7.3f %5d %5d" % (name, h1 / n, h3 / n, wrong, empty))
    print()
    for prev, cur, acc in DIALOGUES:
        s = systems["single"](prev, cur)
        w = systems["weighted"](prev, cur)
        rs = rank_of(s, set(acc), sfx)
        rw = rank_of(w, set(acc), sfx)
        flag = "  ←上下文救回来" if rs > 3 and rw <= 3 else ("  ←反而搞坏" if rs <= 3 and rw > 3 else "")
        print("单轮%-3s 加权%-3s  %s%s" % (
            "-" if rs == 99 else rs, "-" if rw == 99 else rw, cur[:26], flag))


if __name__ == "__main__":
    main()
