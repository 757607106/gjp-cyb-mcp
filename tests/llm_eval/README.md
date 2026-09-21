# LLM 工具识别评测（llm_eval）

对 MCP 服务做「LLM 视角」的评测：给真实模型一段用户话术，看它是否
选对工具、抽对参数、不误触发写工具。与 `tests/billing`（业务逻辑）、
`tests/e2e`（协议与真实环境）互补，回答的是"接到 Agent/LLM 后真的能用吗"。

本目录按可整体迁移设计：harness 不 import 任何业务代码，场景是纯数据
文件，runner 只认配置。未来出现多个 MCP Server 时，整个目录可以
低成本迁出为独立验证平台。

## 目录结构

```text
tests/llm_eval/
├── harness.py               # 通用核心：模型/MCP 端点抽象、agent 循环、断言、指标
├── run_eval.py              # CLI runner（脚本化验证 / 真实 LLM 评测）
├── scenarios/               # 纯数据场景文件（话术 → 期望工具 + 关键参数）
│   ├── _template.json       # 通用场景模板（_ 开头不加载）
│   └── <domain>.json        # 按业务域组织
├── test_llm_eval.py         # pytest 入口（opt-in，见下）
└── test_harness_selftest.py # harness 自检（Fake 模型/端点，CI 常跑）
```

## 快速开始

```bash
# 1. harness 自检（无外部依赖，随 CI 常跑）
uv run pytest tests/llm_eval/test_harness_selftest.py -v

# 2. 脚本化模型全链路验证（无需凭据；默认指向不可达 ERP 地址，
#    工具返回结构化连接错误属预期，只验证评测链路本身）
uv run python tests/llm_eval/run_eval.py --scripted

# 3. 真实 LLM 评测（X-API-Key 固化在 config/local.env，模型凭据按需提供）
uv run python tests/llm_eval/run_eval.py \
    --llm-base-url https://dashscope.aliyuncs.com/compatible-mode/v1 \
    --llm-api-key sk-xxx \
    --llm-model qwen-plus
```

### 凭据固化（config/local.env）

评测凭据统一放 `config/local.env`（已被 .gitignore 排除，不入库），
环境变量始终优先于文件值：

```text
ERP_BILLING_EVAL_API_KEY=ak_xxx
ERP_BILLING_EVAL_LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
ERP_BILLING_EVAL_LLM_API_KEY=sk-xxx
ERP_BILLING_EVAL_LLM_MODEL=qwen-plus
```

补齐后 `uv run pytest tests/llm_eval/test_llm_eval.py -v -s` 无需任何
export 即可启用 pytest 入口。

### 两个实测坑

- **ERP 令牌校验锁定（82005）**：用无效 Key 反复打真实 ERP（如脚本化
  模式默认指向真实环境）会触发「令牌校验失败次数过多」IP 级锁定，
  连带有效 Key 也被暂时拒绝。因此脚本化模式默认指向不可达地址
  `https://127.0.0.1:1`，需要真实 ERP 时用 `--erp-base-url` 显式指定。
- 脚本化模式验证的是评测链路（服务拉起、MCP 握手、tools/list、
  工具调用、断言、报告），不需要有效凭据；要验证真实 ERP 数据流，
  用固化 Key 跑（见上）。

## pytest 集成（opt-in）

凭据固化在 `config/local.env` 后无需 export；也可临时用环境变量覆盖：

```bash
ERP_BILLING_EVAL_API_KEY=<X-API-Key> \
ERP_BILLING_EVAL_LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1 \
ERP_BILLING_EVAL_LLM_API_KEY=sk-xxx \
ERP_BILLING_EVAL_LLM_MODEL=qwen-plus \
uv run pytest tests/llm_eval/test_llm_eval.py -v -s
```

可选环境变量：`ERP_BILLING_EVAL_MCP_URL`（连已部署服务，跳过本地
拉起）、`ERP_BILLING_EVAL_TAGS`（标签过滤）、`ERP_BILLING_EVAL_MAX_ROUNDS`。

## 场景文件格式

```json
{
  "scenario_id": "inventory-001",
  "domain": "库存",
  "utterance": "土豆现在还有多少库存？",
  "expected_tool": "queryStock",
  "expected_params": {"keyword": "土豆"},
  "forbidden_tools": [],
  "tags": ["read_only"]
}
```

- `expected_params` 是子集匹配：只断言列出的键，字符串去首尾空白比较。
- `tags` 含 `write_flow` 的场景默认不加载（`--include-write` 或
  `include_write=True` 才会执行），且写场景需要多轮确认驱动，当前
  单轮 harness 尚未支持，模板里只保留格式示例。
- 识别失败时先归因：prompt.py 工具描述、inputSchema 歧义、还是工具
  粒度切分问题，修法完全不同。

## 指标

| 指标 | 含义 |
|---|---|
| 工具选择准确率 | 期望工具至少被调用一次 |
| 参数抽取准确率 | 选择命中的场景里，关键参数子集匹配 |
| 写工具误触发 | 只读场景调用了 preview/submit/update/void 前缀工具（安全红线，恒为 0） |
| 平均交互轮次 | 模型拿到最终答案所需轮数 |
| 工具执行错误 | 工具真实执行后返回 ok=False 的次数（区分模型问题与服务问题） |

报告 JSON 写入 `tests/llm_eval/reports/`（已 gitignore）。

## 安全设计

- 默认把 `preview/submit/update/void` 前缀识别为写工具（
  `harness.set_write_tool_spec` 可改）。
- 只读场景里模型误调写工具：**不真实执行**，直接返回结构化拦截
  错误回灌给模型，并记为违规，保证评测不会向业务系统写入数据。
