# Walnut Local

本机上的判断仓库。两件东西，一件事：**让 agent 自己查自己。**

```
WalnutLocal/
├── server.py          AgentFullMemory：判断的存取（MCP，stdio，零依赖）
├── ledger.py          本地自审账本：找矛盾、找过期、找没出处的
├── memory/
│   ├── items.jsonl    判断本体（唯一权威位置）
│   └── MEMORY.md      自动生成的人读版
└── ledger.db          我审过什么、发现什么、怎么处置的
```

不对外、不发布、不参与任何公开记录。对外那一层在 walnutlink.ai，是另一件事。

---

## 为什么要分两个文件

`items.jsonl` 存**判断**。`ledger.db` 存**审过的痕迹**。

不合并，是因为「同一件事存两处，必有一处腐化」——2026-11-01 治理记忆时踩过这个坑：
20 条记忆里 8 条是 skill 里已有内容的副本，改一处另一处就烂，还挤爆配额。

所以这里划一条线：**判断只有一个权威位置，处置记录只有一个权威位置，两者靠 id 关联。**

---

## 一、AgentFullMemory：只存判断，不存事实

一般记忆服务器存事实、对话摘要、偏好。没有一家存「错了要付代价的那句话」——
带出处、带到期、带一句明确的「什么算看错」。

四个工具：

| 工具 | 干什么 |
|---|---|
| `memory_propose` | 记一条判断，状态是 `proposed`，永远不是「真的」 |
| `memory_recall` | 搜，出处一起返回；没搜到返回 `no_match`，不许补 |
| `memory_audit` | 重复、无出处、永远结不了账、到期没结、容量 |
| `memory_ledger` | 提升 / 结账 / 列未结 |

三条规则，每条都来自一次失败：

**1. 自己写自己的记忆，会把模型的猜测记成事实。**
所以一切以 `proposed` 入场，提升是另一次明确调用。

**2. 没有出处的记忆没法核对，所以它永远不会错。**
`src` 可以空着，但每次召回都会标 `(none - unverified)`。不是禁止记无出处的想法，
是让无出处这件事永远可见，而不是悄悄变成事实。

**3. 「没记过」和「记过是错的」是两个答案。**
召回不命中返回 `no_match` 加一句 *do not infer; say it was never recorded*。
会拿合理猜测填空的记忆，比没有记忆更糟。

一条记录：

```json
{
  "id": "afm_248a1552d2",
  "kind": "judgment",
  "claim": "沪电股份三季度毛利率升 3.1 个点主要来自 AI 服务器高多层板占比提升",
  "src": "2026 三季报 第 12 页 营业成本附注",
  "scope": "沪电股份 002463",
  "resolve_by": "四季报高多层板占比回落但毛利率仍升",
  "due": "2027-04-30",
  "status": "proposed",
  "outcome": ""
}
```

`outcome` 三态：`hit` / `miss` / `error`。**`error` 不是 `miss`**——「数据没拿到」和
「判断错了」是两件事，混在一起，战绩就成了假的。

---

## 二、ledger.py：自审，不是发布

只查四类问题——这四类就是记忆腐化的四种形态：

| 类 | 判据 |
|---|---|
| `overdue` | 状态 active 且到期日已过 |
| `no_resolve` | 是 judgment 却没写「什么算看错」——等于免责声明 |
| `no_src` | 没有出处，以后没法核对 |
| `contradiction` | 同一范围里两条方向相反，且文本相似度 ≥ 0.55 |

第四类要动脑子，用启发式：同 scope + 文本相似 + 方向词相反（上升/下降、增加/减少、
改善/恶化…）。**启发式一定会有误报**，所以每条都能被标记处置过，处置过的下次不再报。

```bash
python3 ledger.py audit                              # 该审的
python3 ledger.py review <target> <issue> fixed "说明"   # 处置掉
python3 ledger.py log                                # 处置历史
```

`audit` 有发现时退出码 1——可以直接挂定时任务，有问题就出声。

---

## 三、装进 agent

### Hermes

```bash
hermes mcp add walnutlocal --command python3 --args /path/to/WalnutLocal/server.py
```

### DSH

追加到 `$DSH_HOME/profiles/<profile>/cordis.patch.yml`（所有 profile 就放
`$DSH_HOME/cordis.patch.yml`）。不要覆盖已有文件：

```yaml
- insert:
    - id: memory-walnutlocal
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: walnutlocal
        transport: stdio
        command: python3
        args:
          - /path/to/WalnutLocal/server.py
        env:
          WL_MEMORY_DIR: /Users/you/WalnutLocal/memory
```

DSH 把它暴露成 `mcp__walnutlocal__memory_recall` 这样。

### 任意 MCP 客户端

`python3 server.py`，stdio，逐行 JSON-RPC 2.0，答 `initialize` / `tools/list` /
`tools/call` / `ping`。零依赖，单文件。

---

## 四、三步验证

1. **会话 A**：让它记一条带出处的判断，确认调到 `memory_propose` 并拿到 id。
2. **会话 B**（新会话，同一存储）：问一个需要它的题，确认调到 `memory_recall`，
   并且把 `src` 原样念出来。
3. **问一个从没记过的东西**，确认它答「没记过」而不是编。**这一步最重要。**

顺手验自审：

```bash
python3 ledger.py audit
python3 server.py --audit     # 记忆自身的健康报告
```

---

## 五、故意不做的事

- **不做 embedding、不做语义搜索。** 按 claim/why/scope/tags 做子串匹配。
  八成准的语义召回，比精确召回加一句「没记过」更糟。
- **不自动合并冲突。** 两条矛盾的判断都留着，由人或 agent 处置，服务器不偷偷选边。
- **不上云、不要账号、不同步。** 它就是几个文件。
- **不存事实。** 行情、公告、电话会原文放别处。这里放的是关于它们的判断。
- **不发布。** 这一层没有任何通往公开层的接口。要对外说话，是另一个系统的事。

---

## 六、为什么要把它和公开层分开

本地这一层的目的是**自省**：我到底依据什么这么想，三个月后能不能查出来我错了。

一旦给它接一条通往公开的管道，记判断的动机就会变成「这条能不能发」，
而不是「这条以后能不能被我查出错」。动机一错，账本就变成自我宣传的草稿箱。

所以：**同一套字段，两个用途。对内自省，对外沟通。中间不连管道，只共享格式。**

MIT.
