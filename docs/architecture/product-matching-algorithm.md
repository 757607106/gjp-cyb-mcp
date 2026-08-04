# 商品匹配算法与计算公式

最后更新：2026-08-04

本文单独梳理 ERP 开单服务商品匹配链路中的**算法逻辑与计算公式**，聚焦
`erp_billing/matcher.py` 与 `erp_billing/catalog.py`。业务边界与工具契约见
[ai-billing-tools-api-matching.md](./ai-billing-tools-api-matching.md)，本文只讲“怎么算”。

匹配层是**纯确定性算法组合**（图 + 规则级联 + 编辑距离类相似度），不含任何统计
学习或向量运算。全部计算在 MCP 服务内的 `ProductMatcher` 完成，无外部模型依赖。

## 1. 算法全景

| 环节 | 算法 | 位置 | 输出 |
|---|---|---|---|
| 文本预处理 | Unicode NFKC 规范化 + casefold + 正则去噪 | `normalize_name` | 归一化比较键 |
| 同义词组构建 | 无向图连通分量（迭代 DFS） | `_build_alias_groups` | 传递闭包同义词组 |
| 标准名判定 | 频次 + 配置序的多数表决 | `_build_alias_groups` | 组内标准名 |
| 同义词组命中 | 哈希集合交集 | `_score_product` | `alias_exact` 命中 |
| 分级匹配 | 短路优先级级联（决策树） | `_score_product` | 带置信度分数的候选 |
| 模糊打分 | Ratcliff-Obershelp 相似度 | `SequenceMatcher.ratio` | 兜底推荐分数 |
| 候选排序 | 多键稳定排序 | `_candidates` | 有序候选列表 |
| 命中决策 | 唯一直接命中规则 | `_resolve_candidates` | 自动命中 / 推荐分流 |

整条链路的数据流：

```text
keyword ──normalize_name──▶ 归一化键
                              │
        equivalent_names ─────┤  同义词组（图连通分量）
        category_terms ───────┤  品类词集
                              ▼
        每个 product ──▶ _score_product ──▶ MatchCandidate(score, match_type)
                              │
                          _candidates（过滤 + 排序）
                              │
                        _resolve_candidates
                              │
                 ┌────────────┴────────────┐
            唯一直接命中               多候选 / 仅模糊
            （自动写单）              （推荐列表，人工确认）
```

## 2. 文本归一化（比较前提）

所有比较都先经过 `normalize_name`，保证不同书写形式落到同一比较键：

```python
normalized = unicodedata.normalize("NFKC", value).casefold()
key = _DROP_CHARS.sub("", normalized)
```

三步：

1. **NFKC 规范化**：把全角/半角、兼容字符统一（如 `Ａ` → `A`，`（` → `(`）。
2. **casefold**：比 `lower()` 更彻底的大小写折叠，覆盖多语言。
3. **去噪正则**：`_DROP_CHARS` 删除空白与标点
   `[\s\-_./\\()（）【】\[\]{}<>《》,，.;；:：'"“”]`。

> 该结果只用于**比较**，不改变最终写入 ERP 的真实商品名称。

## 3. 分级优先级级联（决策核心）

`_score_product` 对每个商品从高到低依次尝试，**命中即短路返回**，分数编码置信度：

| 顺序 | match_type | 分数 | 判定条件 | 是否自动命中 |
|---|---|---|---|---|
| 1 | `*_exact`（id/code/barcode/name/product_alias/customer_code） | `1.00` | 归一化后**完全相等** | 是（直接命中类型） |
| 2 | `alias_exact` | `0.98` | 同义词组 ∩ 商品标识 ≠ ∅ | 是（别名精确类型） |
| 3 | `alias_contains` | `0.92` | 同义词词（≥2字）是商品名**子串**，且未触发§3.1加工食品过滤 | 否，仅推荐 |
| 4 | `category_contains` | `0.88` | 品类部位词（≥2字）是商品名**子串**，且未触发§3.1加工食品过滤 | 否，仅推荐 |
| 5 | `contains` | `0.86` | 关键词与商品名**互为子串**（正向被过滤时仅保留反向） | 否，仅推荐 |
| 6 | `fuzzy` | 见 §5 | 上述均未命中，取相似度最大值（触发§3.1过滤时 ×0.5 惩罚） | 否，仅推荐 |

分数是**离散置信度标签**而非连续概率：`1.00 / 0.98` 表示等同关系（可自动命中），
`0.92 / 0.88 / 0.86` 表示包含关系（仅推荐），`fuzzy` 为兜底。

### 3.1 加工食品语义过滤

子串匹配（`alias_contains`、`category_contains`、`contains` 正向）命中后，
`_is_likely_mismatch(keyword, product_name)` 会检测该命中是否为语义误匹配。
两条判定规则，满足任一即跳过该匹配类型并降级：

1. **口味修饰后缀**：关键词紧跟 `味` 等口味修饰后缀（如 `番茄` → `番茄味薯片`
   中 `番茄` 仅是口味描述，商品本身是零食而非蔬菜）。
2. **加工食品类型词**：商品名包含不属于关键词本身的加工食品类型词
   （如 `牛肉` → `牛肉面` 中 `面` 不在 `牛肉` 内，产品类型为面食而非生鲜肉）。

加工食品类型词集（`_PROCESSED_TYPE_WORDS`）覆盖面食（`面`）、零食（`薯片`/
`脆片`/`锅巴`/`膨化`）、饼干（`饼干`/`威化`/`曲奇`）、调味品（`酱`/`调料`/
`调味`）、糖果（`糖果`/`果冻`/`布丁`）、饮品（`饮料`/`果汁`/`汽水`）、
茶饮（`奶茶`/`果茶`）、罐头（`罐头`）、酒类（`酒`）和加工肉制品
（`肠`/`丸`/`饺`）。

> **不收录** `干`/`丝`/`片` 等形态词：`牛肉干`/`牛肉丝`/`牛肉片` 仍是
> 该生鲜品类的加工形态，子串匹配为合理推荐。

过滤后匹配降级到 `fuzzy`，且 `fuzzy` 分数施加 `×0.5` 惩罚，使加工食品
在推荐阈值以下被自然排除（如 `牛肉` → `牛肉面` 模糊分 0.8×0.5=0.4 <
`recommendation_score` 0.60，不进入候选）。

> 关键词本身包含类型词时不触发过滤（如 `拉面` → `牛肉拉面` 中 `面` 在
> `拉面` 内，用户搜索的就是面食）。

### 候选过滤阈值

`_candidates` 只保留满足以下任一条件的候选：

$$
\text{keep}(c) =
\begin{cases}
\text{true} & c.\text{match\_type} \in \text{DIRECT} \cup \{\text{alias\_exact}\}\\
\text{true} & c.\text{score} \ge \text{recommendation\_score}\\
\text{false} & \text{otherwise}
\end{cases}
$$

`recommendation_score` 来自 `ErpBillingSettings`，是模糊推荐的准入门槛。

## 4. 同义词组：无向图连通分量

别名配置是有向的 `alias → canonical`（如 `土豆 → 马铃薯`、`洋芋 → 马铃薯`），
但语义是**双向等价**且**可传递**的（土豆 = 马铃薯 = 洋芋）。`_build_alias_groups`
用图连通分量求这个传递闭包。

### 4.1 建图

对每条 `alias → target`，在归一化键之间加**无向边**：

```text
adjacency[alias_key] ∋ target_key
adjacency[target_key] ∋ alias_key
```

得到无向图 $G=(V,E)$，$V$ 为所有归一化名称键，$E$ 为别名等价关系。

### 4.2 迭代 DFS 求连通分量

用显式栈遍历（避免递归深度限制），每个连通分量 $C$ 即一个完整同义词组：

```text
for start in V:
    if start 未访问:
        用栈 DFS 收集 start 所在连通分量 C
        C 内所有键共享同一个同义词组
```

复杂度 $O(|V| + |E|)$。查询时 `equivalent_names(x)` 直接返回 $x$ 所在分量，
组内任意词互相精确匹配。

### 4.3 标准名选举（组内多数表决）

每个连通分量选一个标准名，规则为**被指向频次优先、配置出现序次之**：

$$
\text{canonical}(C) = \arg\max_{k \in C \cap \text{targets}}
\bigl(\text{count}(k),\; -\text{order}(k)\bigr)
$$

- `count(k)`：$k$ 作为别名目标(canonical)被指向的次数，越多越像“大家公认的标准名”。
- `order(k)`：$k$ 首次作为目标出现的顺序，越靠前优先级越高（取负号使小序号胜出）。
- 若分量内无任何 target 键，退化为分量首个元素。

标准名仅用于**解释同义关系**，不替换 ERP 真实商品名。

## 5. 模糊相似度：Ratcliff-Obershelp

前五级全部落空时，`fuzzy` 用 `difflib.SequenceMatcher.ratio()` 兜底打分。其本质是
**Gestalt Pattern Matching / Ratcliff-Obershelp** 算法：递归寻找最长公共子串，再对
两侧剩余部分递归匹配，累加所有匹配块长度。

设两串 $a$、$b$，匹配到的公共块字符总数为 $M$，则相似度：

$$
\text{ratio}(a,b) = \frac{2M}{|a| + |b|} \in [0, 1]
$$

- $2M$：匹配字符在两串中各计一次。
- 分母为两串长度之和，$\text{ratio}=1$ 表示完全相同，$0$ 表示无公共块。
- 平均时间复杂度约 $O(n^2)$。

`fuzzy` 分数取商品名、编号、各别名的相似度最大值：

$$
\text{score}_{\text{fuzzy}} = \max\bigl(
\text{ratio}(q, \text{name}),\;
\text{ratio}(q, \text{code}),\;
\max_{\text{alias}} \text{ratio}(q, \text{alias})
\bigr)
$$

其中 $q$ 为归一化后的查询词，各参与项也均已归一化。

若 §3.1 加工食品过滤判定为语义误匹配，则施加 $\times 0.5$ 惩罚：

$$
\text{score}_{\text{fuzzy}}^{\prime} = \text{score}_{\text{fuzzy}} \times 0.5
$$

使加工食品在推荐阈值以下被自然排除，避免共有字符绕过过滤。

> 注意：Ratcliff-Obershelp 是**字符级顺序敏感**的编辑距离类算法，对错字、局部改写
> 鲁棒，但不理解语义；“薯仔→土豆”这类语义近义无法靠它召回，必须依赖 §4 的人工
> 同义词组数据。

## 6. 集合交集与候选排序

### 6.1 同义词组命中（哈希集合交集）

第 2 级 `alias_exact` 通过集合交集判定：

$$
\text{hit} = \bigl(\text{equivalents}(q)\bigr) \cap \bigl(\text{identifiers}(product)\bigr) \ne \varnothing
$$

`identifiers(product)` 为该商品所有归一化标识（id/code/barcode/name/aliases/
customer_codes）。集合交集平均 $O(\min(m,n))$。

### 6.2 多键稳定排序

候选按四元组升序排序，保证输出确定、可复现：

$$
\text{key}(c) = \bigl(-c.\text{score},\; c.\text{name},\; c.\text{code},\; c.\text{product\_id}\bigr)
$$

分数高者在前；分数相同按名称、编号、ID 字典序，杜绝乱序抖动。

## 7. 命中决策规则

`_resolve_candidates` 决定“自动命中”还是“交人工确认”，分两级判定：

```text
direct = 分数为 *_exact 类型的候选
if direct 非空:
    len(direct) == 1 → 自动命中该商品
    len(direct) >  1 → 全部进推荐列表（同一名称对应多个真实商品，需确认）
else:
    aliases = alias_exact 候选
    if aliases 非空:
        len == 1 → 自动命中；len > 1 → 进推荐列表
    else:
        返回全部候选作为推荐（无自动命中）
```

核心不变式：**只有唯一的等同关系命中（`*_exact` 或 `alias_exact`）才自动写单**；
任何包含匹配、模糊匹配、以及一名多商品的歧义，都必须由用户从推荐列表确认。这是
开单场景“错配零容忍”的算法保证。

## 8. 复杂度小结

设商品数 $n$、查询词长 $L$、同义词组边数 $|E|$：

| 阶段 | 复杂度 | 说明 |
|---|---|---|
| 同义词组构建 | $O(|V| + |E|)$ | 启动/同步时一次性，结果缓存 |
| 单商品打分 | $O(L^2)$（最坏，fuzzy） | 精确/包含命中时短路，远快于此 |
| 单次匹配 | $O(n \cdot L^2)$（最坏） | 遍历全目录，实际多在前几级短路 |
| 候选排序 | $O(n \log n)$ | 稳定多键排序 |

目录为租户内存目录（数百至数千商品级别），单次匹配为毫秒内的纯 CPU 计算。

## 9. 演进方向：embedding + 余弦相似度（未落地）

若要覆盖“无别名数据的语义近义”（如“薯仔→土豆”“巴沙→龙利”），需引入向量化语义
匹配。余弦相似度公式：

$$
\cos(\vec{u}, \vec{v}) = \frac{\vec{u} \cdot \vec{v}}{\|\vec{u}\|\,\|\vec{v}\|}
= \frac{\sum_i u_i v_i}{\sqrt{\sum_i u_i^2}\,\sqrt{\sum_i v_i^2}}
$$

它需要先用 embedding 模型把文本映射为向量，与当前**零模型依赖、确定性**的设计冲突，
且对中文短商品名易产生“语义近但 SKU 不同”的假阳性（如牛腱/牛腩）。因此建议的落点是：

- **不替换** §3 前三级确定性自动命中链路；
- 仅在 `fuzzy` **推荐层**叠加，影响推荐排序，**永不自动命中**；
- 或离线用 embedding 挖掘候选近义词，人工审核后写入 §4 的同义词组数据，
  线上仍保持确定性与零模型依赖。
