# DEC-005 · 模型接入：OpenAI 兼容层 + 配置化 + mock provider

- **状态**：已采纳
- **日期**：2026-10-25
- **范围**：项目级
- **关联**：DEC-001、DEC-006

## 背景

需求里明确「agent 我可以自行绑定解除 api」：模型接口必须是可替换的，不能把某家
SDK 或某个模型名写死在 Agent 里。同时，没有 API Key 的环境（比如评分现场）也要能
把整条流水线跑起来，否则只能看代码不能看运行。

## 可选项

1. **直连官方 SDK**（`openai`、`dashscope`…）：类型友好，但一家一个包、一家一套参数。
2. **统一 OpenAI 兼容协议**（`POST /chat/completions`）+ 配置文件 + `requests`。
3. **只做 mock**：静态脚本跑流程，不接真模型。
4. **本地推理**（llama.cpp / vLLM）：可控，但需要 GPU 与模型文件。

## 决定

选 **方案 2，并把方案 3 作为内置 provider**：
- `core/llm_client.py` 只依赖一条 HTTP 契约，`base_url` / `model` / 温度 全部来自
  `config/agents.json`，可按 Agent 单独覆盖；
- 密钥从 `.env` 的 `AGENT_API_KEY` 读，永不入库；
- `provider=mock` 时按 `mock/` 下的脚本返回内容，与真实 provider 走完全相同的代码路径；
- 解析层容错：`extract_code()` 认三种代码围栏，`extract_json()` 容忍前后废话与
  `json` 围栏，服务端不支持 `response_format` 时也能靠文本解析拿到结构。

## 理由

- 换模型只改配置，三个 Agent 分开配温度（审查用 0 保证结论稳定，编码/修复用 0.1 留一点发挥）。
- 一条 HTTP 契约覆盖 DeepSeek / OpenAI / 通义 / 本地 vLLM，不引入 SDK 版本耦合。
- mock provider 让「演示」和「生产」共用一套编排逻辑，避免出现「演示能跑、真模型跑不通」
  的两套代码；也让 CI 可以在无密钥环境跑端到端。

## 代价与后果

- 不同厂商的兼容层有细微差异（`max_tokens` vs `max_completion_tokens`、
  `response_format` 支持情况），需要在使用时验证，必要时在配置里加映射。
- 没有 SDK 的流式输出、函数调用封装；如果要 tool use，需要自己拼 `tools` 字段。
- mock 场景是写死的脚本，只能证明「编排正确」，不能证明「模型能力」——这点在
  README 里显式说明，避免误读（`--live` 才是真实链路）。

## 复核条件

若需要 function calling / 结构化输出强约束 / 多模态，评估换成官方 SDK 或多协议适配层。
