# mock/ —— 离线脚本化场景的数据

`provider=mock` 时，`core/llm_client.py` 里的 `MockProvider` 不调用任何网络接口，
而是按「第几次调用」返回这里的文件，模拟一次真实的
「编码 → 审查打回 → 修复 → 审查通过」过程。

| 文件 | 对应调用 | 内容 |
| --- | --- | --- |
| `coder_r1.py` | coder 第 1 次 | 故意有缺陷的初稿：无锁、过期不删、覆盖不刷新 TTL、`min()` 扫全表 |
| `reviewer_r1.json` | reviewer 第 1 次 | 驳回结论：6 条问题，分别对应 REQ-2/3/4/5/6 |
| `fixer_r1.py` | fixer 第 1 次 | 修复后的完整实现（与 `solutions/ttl_cache.py` 同步） |
| `reviewer_r2.json` | reviewer 第 2 次 | 通过结论：97 分，1 条 low 建议 |

## 为什么要 mock

1. **无 Key 也能演示**：评分/演示环境不一定有 API Key，mock 让整条流水线（状态机、
   消息总线、自动验收、DEC 记录、报告生成）都能真实跑一遍。
2. **可复现**：每次运行结论一致，便于截图、测试编排逻辑本身。
3. **不是替代品**：mock 只证明「编排正确」，不证明「模型能力」。真实链路请用 `--live`。

## 注意

`fixer_r1.py` 是 `solutions/ttl_cache.py` 的快照。如果改了参考实现，请一并更新：

```powershell
Copy-Item solutions\ttl_cache.py mock\fixer_r1.py -Force
```

`coder_r1.py` 是刻意留坑的初稿，**不要**修它——它的缺陷是演示所需。
