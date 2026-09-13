# 兼容性和开发

## 架构

- `source.py`：常见平台路径发现、文件头识别、只读 SQLite/SQLCipher 事务、消息快照语义。
- `convert.py`：标准化源会话到 Codex rollout；同时生成模型 `response_item` 和原生 `item_completed`。
- `bundle.py`：原始归档、可复现校验、导入收据、重复导入与回滚。
- `verify.py`：固定版本 Codex app-server，临时空项目和本机 Responses 模拟服务，分页原生历史和实际下一轮请求验证。
- `cli.py`：`doctor`、`list`、`plan`、`convert`、`verify`、`install`、`rollback`。

没有数据库写入式“索引补丁”。正式导入只添加 rollout，首次 `codex resume <id>` 让 Codex 自己建立索引。

## 已验证与未验证

本次本地运行环境是 macOS arm64 / Python 3.9.6 / Codex 0.154.0：

- 合成会话的用户消息、助手消息、可见思考、命令、动态工具、MCP、文件变化的原生数量、内容和顺序。
- 双会话恢复、MCP 错误、文件删除/重命名、未知工具耗时。
- 首次索引、进程重启后的读取/恢复、模拟模型下一轮的历史消息/工具参数/结果。
- 隔离目录安装、回滚；合成 SQLite/WAL、篡改/路径保护、中断恢复等标准库测试。

不是以下事项的验收：

- 真实 TRAE 加密主库的内容提取、真实 TRAE 版本全覆盖。
- Codex 桌面版左侧列表、差异控件、图片 UI。
- 在线模型是否接受所有历史工具名、无签名 reasoning，以及超长历史的保留策略。
- Windows / Linux 实机测试。仓库提供 CI 矩阵，只有 CI 实际通过后才能宣称这些平台已经测试。

## 安全边界

源数据库用 `mode=ro`、`query_only` 和读取事务，不使用忽略 WAL 的 `immutable=1`。SQLite 自身可能触及已有共享内存锁；优先使用关闭 TRAE 后取得的一致性副本。只导出所选会话数据，不读取登录密钥或其他应用凭据。

验证进程使用环境变量白名单，隔离 HOME、CODEX_HOME、SQLite 和配置目录，禁用 Git 全局配置，不继承 Token 或代理配置。不复制用户的 MCP/Skill/认证配置。模拟模型只返回固定回复，不产生新工具调用，收到工具/审批 RPC 会拒绝。

迁移数据仍是敏感明文。POSIX 目录权限 0700、文件 0600；Windows 应使用当前用户专属 ACL 目录。原子发布依赖目标文件系统的硬链接能力，不支持时直接失败，不回退为可能覆盖旧文件的写法。不要使用共享可写目录、网络盘或攻击者可替换路径的目录。

迁移锁不等于 Codex 的全局锁。安装/回滚前需关闭目标 Codex，备份目标目录。不会回滚已经变化的文件；失败时保留收据和完整文件，可重试。不会直接删除 Codex 的 SQLite 索引、线程关联或历史引用。

## 扩展适配器

新增 TRAE 版本应先提供无敏感信息的合成/脱敏结构样本、表结构和字段证据。不要把真实聊天、密钥、数据库、日志或用户本机路径放到测试夹具/Issue。

新增映射要求：

1. 有可追溯的源字段依据；未知字段不能静默忽略为“无损”。
2. 原生显示事件和模型历史同时维护，调用 ID、轮次、顺序一致。
3. 为不支持的数据提供可见损失说明或拒绝，不推断历史执行成功。
4. 通过离线集成测试的首次恢复、重启恢复、下一轮模型输入验证。
5. 更新兼容矩阵；不移除版本检查来假装支持。

新增 Codex 版本需要重新读取该版本协议、运行二进制验证，不能只把 `CODEX_VERSION` 改为 latest。当前用到的内部 rollout 格式和部分分页接口不是面向第三方的稳定导入 API。

## 分发

`python -m build` 生成 wheel 和源码包。`MANIFEST.in` 限定源码分发文件，排除研究目录、迁移包和用户原始记录。普通分支 CI 只构建 Actions Artifact；推送与包版本一致的 `vX.Y.Z` Tag 后，Release Job 仅在单元测试、三平台 Codex 集成测试和构建全部成功后运行，发布 wheel、源码包和 `SHA256SUMS` 到 GitHub Releases。流程不上传 PyPI、不发布聊天数据；PyPI 名称可用性尚未核验。

发布前应执行：

```bash
python -m unittest discover -s tests -v
python -m build
python -m twine check dist/*
```

集成测试另设置 `TRAE2CODEX_TEST_CODEX`。分享仓库/安装包之前确认 `.local`、`.research`、数据库、环境变量、原始聊天未被加入 Git。不要用 `git add -f` 包含这些路径。

创建版本发布：

```bash
git tag -a v0.1.0 -m "trae2codex v0.1.0"
git push origin v0.1.0
```

Tag 必须精确等于 `v` 加 `pyproject.toml` 与 `trae2codex.__version__` 中的版本；不匹配时 Release Job 会拒绝发布。GitHub Release 使用 `contents: write` 的最小 Job 权限，其他 Job 保持只读。

## 协议依据

- [TRAE 官方日志与 SessionID 说明](https://docs.trae.cn/ide_get-logs-or-session-id)：日志不保证完整历史。
- [Codex 0.154.0 core protocol](https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/protocol/src/protocol.rs)
- [Codex 原生 TurnItem](https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/protocol/src/items.rs)
- [Codex 模型历史](https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/protocol/src/models.rs)
- [Codex rollout 持久化](https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/rollout/src/recorder.rs)
- [Codex thread API](https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/app-server-protocol/src/protocol/v2/thread.rs)

源数据库候选结构另参考社区的 `Oh-My-Trae/trae-db-decrypt` 表结构说明，该说明不是官方格式契约；本项目没有复制其进程内存扫描/解密实现，也不使用他人的密钥。
