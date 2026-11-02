#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""判断库的索引表（三级：上下文 → 索引表聚焦 → 才检索）

为什么要有索引表
----------------
现在的 memory_context 每次把用户这句话跟**全部**判断逐条算分。68 条时 30ms 看不出来，
一万条时就是每轮几百毫秒、而且大部分算分都浪费在跟这句话毫无关系的条目上。

人不是这样：听到「沪电」先想起"我管着沪电"这件事（O(1) 的一跳），
然后才在这一小撮里比较。索引表就是把这一跳做出来，把候选集从 N 收敛到几十。

四层结构
--------
1 实体表   entity -> {id}         哈希，O(1)。别名（沪电 / 沪电股份 / 002463）指向同一个键
2 词组表   bigram -> {id}         带 IDF。df > 30% 的二元组直接不进表（数据自己长出来的停用词）
3 时间表   due / created 有序      拿"欠着的账"不用扫
4 关系边   实体共现 -> 权重        一跳扩散。提到"存储涨价"，想起美光/佰维/德明利那几条

打分
----
覆盖度用 IDF 加权：Σidf(命中) / Σidf(问题)。这一步是效率的关键——
「毛利率」「判断」这种到处都有的词，idf 接近 0，压不动分数；
只有「沪电」「Lumentum」「91MW」这种稀有词才能把某条顶出来。

两条打平时不猜
--------------
top1 - top2 < 0.04 时返回两条并明说分不清。这是 tip-of-the-tongue：
人想不起是哪个的时候会说"是不是A……也可能是B"，不会硬编一个。
"""

import json
import math
import os
import re
import time
from collections import defaultdict

HOME = os.path.expanduser("~")
DATA_DIR = os.environ.get("WL_MEMORY_DIR") or os.path.join(HOME, "WalnutLocal", "memory")
ITEMS = os.path.join(DATA_DIR, "items.jsonl")

# 中文公司/机构名的后缀。不做分词，靠后缀反向切出词干。
SUFFIX = (
    "股份", "科技", "集团", "控股", "电子", "通信", "半导体", "材料", "新材",
    "光电", "微电", "能源", "生物", "医药", "汽车", "重工", "机械", "电器",
    "电气", "精密", "智能", "数据", "网络", "软件", "信息", "实业", "国际",
    "有限", "公司", "工厂", "电子科技", "微电子",
)
# 记录里反复出现的套话。建索引前先剥掉，否则任何问"算不算看错"的问题，
# 都会把这些套话的载体顶到第一（2026-11-02 实测：问沪电，浮上来的是存储那条）。
META = ("什么情况算我看错", "什么情况下算我看错", "什么算看错", "算什么我看错",
        "什么情况我错了", "什么情况下我错了", "这条判断就错", "那这条判断就错",
        "这条就错了", "我就错了", "什么情况算错", "什么算我错")

GENERIC_DF = 0.30      # 出现在 30% 以上条目里的实体/词，当通用词处理
MAX_CAND = 60          # 候选集上限（精排只算这么多）
TIE = 0.04             # 分差小于这个值，判定"分不清"
FLOOR = 0.22

LATIN = re.compile(r"[A-Za-z][A-Za-z0-9\-\.]{1,15}")
CODE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
NUMUNIT = re.compile(r"\d+(?:\.\d+)?\s*(?:%|亿|万元|亿元|MW|GW|倍|个点|个百分点)")


def _bigrams(s):
    """中文用二元组，拉丁词整词。丢掉纯数字和标点。"""
    s = (s or "").lower()
    out = set()
    for w in LATIN.findall(s):
        if len(w) >= 2:
            out.add(w)
    zh = re.sub(r"[^\u4e00-\u9fa5]+", " ", s)
    for seg in zh.split():
        for i in range(len(seg) - 1):
            out.add(seg[i:i + 2])
        if len(seg) == 1:
            out.add(seg)
    return out


# 虚词表：专名不会以这些字开头或结尾。这是判断"这串字是指称还是谓述"的最低成本办法。
FUNC = set("的了是在和与也都就这不那个把被给对从到会能要有没我你他它说看做成为以及或但而吗呢吧还又很太更最着过得进出去来些么自己将并且如果虽然因为所以然后而且")

def _nameish(g):
    """像不像专名：不夹虚词、内部二元组不常见。"""
    if not g:
        return False
    if g[0] in FUNC or g[-1] in FUNC:
        return False
    return True


def _strip_meta(t):
    for m in META:
        t = t.replace(m, " ")
    return t


def _ngrams(text, ns=(4, 3)):
    """3-4 字的中文串。用来从库里自己长专名表，不靠人工词典。"""
    out = set()
    zh = re.sub(r"[^\u4e00-\u9fa5]+", " ", (text or ""))
    for seg in zh.split():
        for n in ns:
            for i in range(len(seg) - n + 1):
                out.add(seg[i:i + n])
    return out


def harvest_entities(text):
    """从一段话里抽实体。返回 {canonical: original_form}。

    三路：六位代码 / 拉丁词 / 中文后缀词干。
    词干取后缀前的 2-4 字：'沪电股份' -> '沪电'，'胜宏科技' -> '胜宏'。
    """
    t = text or ""
    got = {}

    for c in CODE.findall(t):
        got.setdefault(c, c)

    for w in LATIN.findall(t):
        if w.lower() not in ("ai", "pcb", "dsp", "the", "and", "for", "not", "was"):
            got.setdefault(w.lower(), w)

    for suf in SUFFIX:
        for m in re.finditer(suf, t):
            i = m.start()
            for back in (4, 3, 2):
                if i - back >= 0:
                    stem = t[i - back:i]
                    if re.fullmatch(r"[\u4e00-\u9fa5]+", stem):
                        got.setdefault(stem, stem + suf)
                        break
    return got


class JudgmentIndex:
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else self._load()
        self.N = max(1, len(self.rows))
        self.by_id = {r["id"]: r for r in self.rows}
        self.ent_index = defaultdict(set)
        self.ent_df = defaultdict(int)
        self.ent_alias = {}            # 任意写法 -> canonical
        self.post = defaultdict(set)
        self.idf = {}
        self.graph = defaultdict(lambda: defaultdict(float))
        self.key_index = defaultdict(set)   # 库里自己长出来的专名表（3-4字串 + 长拉丁词 + 代码）
        self.key_df = defaultdict(int)
        self.due = []
        self._build()

    def _load(self):
        if not os.path.exists(ITEMS):
            return []
        out = []
        with open(ITEMS, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        pass
        return out

    # ---------- 建表 ----------
    def _build(self):
        t0 = time.time()
        df_bg = defaultdict(int)
        for r in self.rows:
            rid = r["id"]
            text = _strip_meta(" ".join(str(r.get(k) or "") for k in
                            ("claim", "scope", "why", "tags", "resolve_by")))
            for bg in _bigrams(text):
                self.post[bg].add(rid)
                df_bg[bg] += 1
            for canon, form in harvest_entities(" ".join(str(r.get(k) or "") for k in ("claim", "scope"))).items():
                self.ent_index[canon].add(rid)
                self.ent_df[canon] += 1
                self.ent_alias[canon] = canon
                self.ent_alias[form] = canon
            for c in CODE.findall(str(r.get("scope") or "")):
                self.ent_alias[c] = self.ent_alias.get(c, c)
            if r.get("due"):
                self.due.append((str(r["due"]), rid))

        # IDF
        for bg, ids in self.post.items():
            df = len(ids)
            self.idf[bg] = math.log((self.N + 1) / (df + 0.5))
        # 通用词门槛：不分词也能长出停用词表
        self.stop_bg = {bg for bg, df in df_bg.items() if df > self.N * GENERIC_DF}
        self.stop_ent = {e for e, df in self.ent_df.items() if df > self.N * GENERIC_DF}

        # 专名表（从数据里长出来，不靠人工词典）
        # 判据：一个 2-5 字串只在一两个条目里出现过，首尾不是虚词，且不是更长的同一个串的一部分。
        # 为什么不是"3-4 字 + 少见"：那样会把"还能不能""那份财报"当专名。
        # 2026-11-02 实测：换成"唯一性 + 虚词边界"之后，10 个无关问题只有 1 个能挤进来。
        span_cap = max(2, int(self.N * 0.02))
        sdf = defaultdict(int)
        spost = defaultdict(set)
        for r in self.rows:
            text = " ".join(str(r.get(k) or "") for k in ("claim", "scope", "why", "tags", "resolve_by"))
            seen = set()
            for n in (5, 4, 3, 2):
                for g in _ngrams(text, (n,)):
                    seen.add(g)
            for w in LATIN.findall(text):
                if len(w) >= 3:
                    seen.add(w.lower())
            for c in CODE.findall(text):
                seen.add(c)
            for g in seen:
                sdf[g] += 1
                spost[g].add(r["id"])
        self.span_df = {g: d for g, d in sdf.items() if d <= span_cap}
        # 话题名：出现得不少、但也谈不上通用的 4-5 字串（"中际旭创"这种）。
        # 它进不了键表（出现太多次就不算"稀有"），但用户提到它时，
        # 应该只在那几条里比较——名字是硬条件这条原则，对高频名字同样成立。
        topic_hi = max(3, int(self.N * 0.30))
        self.topic_index = {g: spost[g] for g, d in sdf.items()
                            if 3 <= d <= topic_hi and len(g) >= 4
                            and re.fullmatch(r"[\u4e00-\u9fa5]{4,5}", g) and _nameish(g)}
        # 2 个字的串太容易偶然凑上（"问题""东西"在 68 条的库里都只出现一两次，
        # 但它们不是键）。所以中文键最少 3 个字；2 个字的只留实体表里的名字和代码。
        for g, d in self.span_df.items():
            if re.fullmatch(r"[\u4e00-\u9fa5]{2,5}", g):
                if not _nameish(g):
                    continue
                if len(g) == 2 and g not in self.ent_index:
                    continue
            self.key_index[g] = spost[g]
            self.key_df[g] = d
        self._span_cap = span_cap

        # 关系边：同一条里出现的实体两两相连
        for r in self.rows:
            ents = list(harvest_entities(" ".join(str(r.get(k) or "") for k in ("claim", "scope"))).keys())
            for i in range(len(ents)):
                for j in range(i + 1, len(ents)):
                    a, b = ents[i], ents[j]
                    self.graph[a][b] += 1.0
                    self.graph[b][a] += 1.0
        # 预计算每条记录的两样东西。score() 里现算的话，5 万条时每轮几万次
        # 正则 + 集合运算，索引表省下的时间会被这里吃回去（实测 116ms）。
        # 建索引时算一次，之后每轮只查表。
        self._rbg = {}
        self._rent = {}
        for r in self.rows:
            t = _strip_meta(" ".join(str(r.get(k) or "") for k in
                                     ("claim", "scope", "why", "tags", "resolve_by")))
            self._rbg[r["id"]] = _bigrams(t)
            self._rent[r["id"]] = set(harvest_entities(
                " ".join(str(r.get(k) or "") for k in ("claim", "scope"))).keys())

        self.due.sort()
        self._build_time = round(time.time() - t0, 4)

    # ---------- 查询：先收敛，再精排 ----------
    def _resolve_entities(self, text):
        """把问题里的写法映射到 canonical。

        同样只枚举问题自身的子串（2-6 字），不遍历别名表——
        遍历别名表在 20,000 条时会变成新的 O(N)。
        """
        low = (text or "").lower()
        found = {}
        for n in (6, 5, 4, 3, 2):
            for g in _ngrams(low, (n,)):
                c = self.ent_alias.get(g)
                if c and c not in found:
                    found[c] = g
        for w in LATIN.findall(low):
            c = self.ent_alias.get(w)
            if c and c not in found:
                found[c] = w
        for code in CODE.findall(low):
            c = self.ent_alias.get(code)
            if c and c not in found:
                found[c] = code
        return found

    def key_spans(self, text):
        """问题里出现了哪几个"这库认得的名字"。取最长的、互不重叠的。

        这是整个索引表的入口：先在这里把问题收敛到几个键上，
        后面的候选集只从这些键的倒排链里出，不扫全库。
        """
        low = (text or "").lower()
        spans = set()
        for g in self.key_index:      # 只认过滤后的专名表，别去扫原始 n-gram

            if len(g) < 2:
                continue
            i = low.find(g.lower())
            if i >= 0:
                spans.add(g)
        kept = []
        for g in sorted(spans, key=len, reverse=True):
            if any(g in k for k in kept):
                continue
            kept.append(g)
        return kept[:6]

    def topic_spans(self, text):
        """问题里出现的高频话题名（≥4 字）。只枚举问题自身子串，不扫表。"""
        low = (text or "").lower()
        out = []
        for n in (5, 4):
            for g in _ngrams(low, (n,)):
                if g in self.topic_index and g not in out:
                    out.append(g)
        return out

    def keys_in(self, text):
        return self.key_spans(text)

    def candidates(self, text, cap=MAX_CAND):
        """L1：把候选收敛到 cap 以内，工作量跟库的大小无关。

        2026-11-02 实测教训：原来 `cand |= self.post[b]` 碰上 5,000 条的大倒排链，
        一次性把整个候选集撑爆——索引表省掉的扫描又被自己加了回来（20k 条 116ms）。
        现在从最稀有的词开始取，遇到超过预算的链直接跳过，够了就停。
        """
        ents = self._resolve_entities(text)
        cand = set()
        for e in ents:
            ids = self.ent_index.get(e)
            if not ids:
                continue
            if len(cand) + len(ids) > cap:
                continue
            cand |= ids
        if len(cand) < cap // 2:
            for e in ents:
                for nb, w in self.graph.get(e, {}).items():
                    if w >= 2 and nb not in ents:
                        ids = self.ent_index.get(nb) or ()
                        if len(cand) + len(ids) <= cap:
                            cand |= set(ids)

        qbg = [b for b in _bigrams(text) if b not in self.stop_bg and b in self.post]
        qbg.sort(key=lambda b: -self.idf.get(b, 0))      # 稀有的先取
        for b in qbg:
            if len(cand) >= cap:
                break
            ids = self.post[b]
            if len(ids) > cap:                            # 太普遍，取它对候选没帮助
                continue
            cand |= ids
        return cand, ents, qbg

    def _cov_ent(self, text, r, ents=None, qbg=None):
        """一句话跟一条判断「像不像」。不含时间和状态，多段合并时只算一次时间。"""
        ents = ents if ents is not None else self._resolve_entities(text)
        qbg = qbg if qbg is not None else [b for b in _bigrams(text) if b not in self.stop_bg]
        rbg = self._rbg.get(r["id"]) or set()

        # 分母必须把「库里根本没有的词」算进去，而且按最强权重算。
        # 2026-11-02 实测 bug：只算库里存在的词，问「今天天气怎么样」时
        # 六个二元组只有一个恰好撞上，于是覆盖度 = 100%，10 个无关问题触发 6 个。
        # 索引表的核心不是"我有什么"，是"我知道我没有什么"。
        idf_unseen = math.log((self.N + 1) / 0.5)
        tot = sum(self.idf.get(b, idf_unseen) for b in qbg) or 1e-6
        hit = sum(self.idf.get(b, 0.0) for b in qbg if b in rbg)
        cov = min(1.0, hit / tot)
        rare = sum(1 for b in qbg if b in rbg
                   and len(self.post.get(b, ())) <= max(2, self.N * 0.05))

        rent = self._rent.get(r["id"]) or set()
        ent = 0.0
        hitent = []
        for e, form in ents.items():
            if e in rent:
                ent = max(ent, 0.15 if e in self.stop_ent else 0.55)
                hitent.append(form)
        if ent == 0.0:
            for e in ents:
                for nb, w in self.graph.get(e, {}).items():
                    if nb in rent and w >= 2:
                        ent = max(ent, 0.22)
                        hitent.append("~" + nb)
                        break
        return cov, ent, hitent, rare

    def _adjust(self, s, r):
        """时间和状态。跟「像不像」分开算，多段合并后只乘一次。"""
        age_d = 0.0
        try:
            age_d = max(0.0, (time.time() - float(r.get("created") or 0)) / 86400.0)
        except Exception:
            pass
        s *= (0.78 + 0.22 * max(0.0, 1 - age_d / 180.0))
        st = r.get("status")
        s *= {"active": 1.12, "settled": 1.04, "dropped": 0.2}.get(st, 1.0)
        overdue = False
        if st == "active" and r.get("due"):
            try:
                overdue = str(r["due"]) < time.strftime("%Y-%m-%d")
            except Exception:
                pass
            if overdue:
                s *= 1.15
        return s, overdue

    def score(self, text, r, ents=None, qbg=None):
        cov, ent, hitent, rare = self._cov_ent(text, r, ents, qbg)
        s, overdue = self._adjust(0.62 * cov + 0.38 * ent, r)
        why = []
        if cov > 0.05:
            why.append("字面 %.2f" % cov)
        if hitent:
            why.append("提到 " + "、".join(hitent[:3]))
        if overdue:
            why.append("已过期")
        return round(min(s, 1.0), 3), "｜".join(why)

    def query(self, text, top_k=3, floor=FLOOR):
        return self.query_multi([(text, 1.0)], top_k=top_k, floor=floor)

    def query_multi(self, parts, top_k=3, floor=FLOOR,
                    gate_cov=0.45, gate_rare=3, keys_only=False):
        """多段一起想：用户这句话是主语，我上一轮在想什么只是线索。

        parts = [(文本, 权重), ...]，第一段是用户当前这句。
        2026-11-02 加这一层，来源是 AgentIR 那篇：检索该用 agent 的推理过程做查询，
        不只是用户那句话。但直接拼成一坨是错的——我试过，长上文会把短问题淹掉
        （历史每段 400 字，用户这句只有十几个字，字面覆盖全被上文占了）。
        所以按段算分再加权：当前这句 1.0，我上一轮 0.4。
        """
        parts = [(t, w) for t, w in (parts or []) if t and len(str(t).strip()) >= 4]
        if not parts:
            return {"ok": True, "triggered": [], "note": "太短，不值得想", "ms": 0}
        t0 = time.time()
        main = parts[0][0]

        cand = set()
        entsets, qbgs = [], []
        for i, (text, w) in enumerate(parts):
            c, e, q = self.candidates(text, cap=MAX_CAND if i == 0 else MAX_CAND // 2)
            cand |= c
            entsets.append(e)
            qbgs.append(q)

        # 专名硬条件：先看用户这句里的名字；这句没有，才用上文的。
        def hard_of(es):
            return {e for e in es if e in self.ent_index
                    and len(self.ent_index[e]) <= max(2, self.N * 0.05)}
        hard = hard_of(entsets[0]) or set().union(*[hard_of(e) for e in entsets[1:]]) if len(entsets) > 1 else hard_of(entsets[0])
        keep = set()
        for e in hard:
            keep |= set(self.ent_index.get(e) or ())
        if not keep:
            # 没有实体表里的名字，就看问题上有没有"话题名"（中际旭创这类高频名字）
            for g in self.topic_spans(main):
                keep |= set(self.topic_index.get(g) or ())
            if keep:
                cand = (cand & keep) or keep
        elif keep:
            cand = (cand & keep) or keep

        keys_any = set()
        for text, w in parts:
            keys_any |= set(self.key_spans(text))
            keys_any |= set(self.topic_spans(text))   # 高频名字也是键

        # 合并方式：取"谁更像就用谁"，不给权重做除法。
        # 2026-11-02 实测教训：原来按 1.0/1.4 加权平均，上文的证据被稀释掉，
        # 12 条追问只中 7 条，反而不如直接把上文拼成一坨（中 11 条）。
        # 正确的直觉是：当前这句有信号就听它的，没信号才让上文接住。
        PREV_DISCOUNT = 0.85
        scored = []
        for rid in cand:
            r = self.by_id.get(rid)
            if not r:
                continue
            best = 0.0
            main_cov = main_rare = 0.0
            cov_gate = rare_gate = 0.0
            why = []
            for i, ((text, w), ents, qbg) in enumerate(zip(parts, entsets, qbgs)):
                cov, ent, hitent, rare = self._cov_ent(text, r, ents, qbg)
                part_s = 0.62 * cov + 0.38 * ent
                part_s *= (1.0 if i == 0 else PREV_DISCOUNT)
                if part_s > best:
                    best = part_s
                if i == 0:
                    main_cov, main_rare = cov, rare
                if cov > cov_gate:
                    cov_gate, rare_gate = cov, rare
                if hitent:
                    why.append(("" if i == 0 else "上文提到 ") + "、".join(hitent[:3]))
            s, overdue = self._adjust(best, r)
            if s < floor:
                continue
            # 触发闸：要么问题里有这库认得的键（任一段），要么某一段的稀有词撞得够多。
            if keys_only:
                if not keys_any:
                    continue
            elif not (keys_any or (cov_gate >= gate_cov and rare_gate >= gate_rare)):
                continue
            head = []
            if main_cov > 0.05:
                head.append("字面 %.2f" % main_cov)
            head.extend(why)
            if overdue:
                head.append("已过期")
            scored.append((round(min(s, 1.0), 3), "｜".join(head), r))

        scored.sort(key=lambda x: -x[0])
        tie = len(scored) >= 2 and (scored[0][0] - scored[1][0]) < TIE
        out = []
        for s, why, r in scored[:top_k]:
            out.append({"id": r["id"], "claim": r.get("claim", ""),
                        "src": r.get("src", ""), "resolve_by": r.get("resolve_by", ""),
                        "due": r.get("due", ""), "status": r.get("status", ""),
                        "score": s, "why": why})
        return {"ok": True, "triggered": out,
                "others_related": max(0, len(scored) - len(out)),
                "ambiguous": tie, "candidates": len(cand),
                "ms": round((time.time() - t0) * 1000, 1)}

    def stats(self):
        return {
            "judgments": self.N,
            "entities": len(self.ent_index),
            "bigrams": len(self.post),
            "stop_bigrams": len(self.stop_bg),
            "stop_entities": len(self.stop_ent),
            "edges": sum(len(v) for v in self.graph.values()),
            "build_ms": round(self._build_time * 1000, 1),
        }


if __name__ == "__main__":
    import sys
    ix = JudgmentIndex()
    print(json.dumps(ix.stats(), ensure_ascii=False, indent=1))
    if len(sys.argv) > 1:
        print(json.dumps(ix.query(" ".join(sys.argv[1:])), ensure_ascii=False, indent=1))
