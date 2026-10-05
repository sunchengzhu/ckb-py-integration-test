# CKB PR 5300：ai-test-agent 本地成品快照

本分支保存按 AI Test Agent v2 完成的 rich-indexer 过滤区块回滚测试产物，供分享和代码 review。产品修改来自 [CKB PR #5300](https://github.com/nervosnetwork/ckb/pull/5300)。

## 比较基线与文件范围

测试工程 [PR #145](https://github.com/nervosnetwork/ckb-py-integration-test/pull/145) 的合并提交为 `d25aa455fe461d565b15bf24c58ec4f2a7f24e78`，其唯一父提交、合并前基线为 `4b520d7cb603ab66f0e17232d285ecb2d507ea3f`。本轮原始执行所在的本地提交 `d3f8a00e1693e1e11daa7950182b68e0e4ed5232` 的 tree 与该基线相同。

从该基线比较，本快照新增 35 个已有产物与输入入口文件和本说明，共 36 个文件；不包含 PR #145 中的 Alert 测试。查看完整差异：

```sh
git diff --stat 4b520d7cb603ab66f0e17232d285ecb2d507ea3f codex/rich-indexer-ai-test-agent-demo
git diff --name-status 4b520d7cb603ab66f0e17232d285ecb2d507ea3f codex/rich-indexer-ai-test-agent-demo
```

| 目录 | 文件数 | 内容 |
| --- | ---: | --- |
| `reviews/` | 2 | 行为、Spec、测试树、6 个 Case；原始人工修正措辞 |
| `suites/rpc/` | 6 | 测试、逐实例结果记录器、4 个 fixtures |
| `reports/ckb-rich-indexer-rollback-runner-snapshot/` | 24 | 固定输入描述、设计 review/响应、覆盖证据、注释校验、收集与执行记录、Gate 2、记录器 probe 和环境清理 |
| `.ai-test-agent/inputs/` | 3 | review 直接引用的固定输入入口与原始清单 |
| `docs/sharing/` | 1 | 本说明 |

## 阅读顺序

1. `reviews/ckb-pr-5300-rich-indexer-rollback.md`：变更解释、Spec、测试树和五列 Case 表。
2. `reports/ckb-rich-indexer-rollback-runner-snapshot/design-review.json` 与 `design-response.json`：B 对六个 Case/Spec 的确认以及 A 单独响应。
3. `suites/rpc/tests/test_rich_indexer_rollback.py`：六个 `TEST-MAP` 和由证据生成的中英双语 `TEST-EVIDENCE` 注释。
4. `reports/ckb-rich-indexer-rollback-runner-snapshot/coverage.md`：B 对实际触发、观察及断言的覆盖判断。
5. 同目录 `execution.log`、`runner-results.json` 与 `acceptance.json`：真实执行、逐实例状态和 Gate 2。

## 已保存的执行事实

Scope：`ckb-rich-indexer-rollback-runner-snapshot`。六个 Case 在 PostgreSQL/SQLite 上展开为 14 个参数实例；原始执行为 `14 passed in 37.44s`，退出码 0。六个 Case 均为 `mapped / covered / current / passed`。Gate 2 保留 `needs_decision`：B 使用不带 A 历史的新会话，但宿主隔离未获证明（`isolation_unverified`）。这些结果只支持所选行为范围。

以上 JSON、日志、review、测试及注释按本地最终文件原样封存。报告记录的是原始执行快照及指纹；本分支的提交是归档操作，没有重新执行测试，也没有把原始指纹改写为归档后的提交。`current` 是原始快照结论，不能直接替代新 checkout 的证据检查。

## 运行环境边界

本分支保留测试与 fixtures，不包含本机二进制、构建目录、虚拟环境、临时数据库连接配置或 `.ai-test-agent/current/` 状态。`input-index.json` 保留原始输入路径及哈希；仅保留三份 review 直接引用的固定输入入口清单；清单所列的全部原始源码/材料、工具快照、插件和运行时等外部本机材料没有一并归档。

重新执行时需另行准备并固定环境：产品 head `33e4eec7502b754ddf61282c04e0a76a29e27b26`（原始 CKB 二进制 SHA-256 `3d50a5c1eada3ba703af2f0122b0cafe8ee1ac1733eb4c99a66a5f2979d64c34`）、ckb-cli、PostgreSQL、SQLite、项目 Python 依赖及 `psycopg`。测试读取 `suites/rpc/.runtime/ckb5300-runtime.json`；该配置包含本机路径和临时连接信息，应在新环境单独准备。结果记录器在官方 runner 设置 `AI_TEST_AGENT_RESULT` 时输出逐实例结果。

```sh
venv/bin/python -m pytest -c pytest.ini -vv suites/rpc/tests/test_rich_indexer_rollback.py
```

新环境执行应生成新 scope 的输入、review 和执行证据，不覆盖本目录的原始归档。Case 的实质预期变动仍需回到 Gate 1 确认。
