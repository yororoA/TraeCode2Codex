# trae2codex

将可读取的 TRAE 会话转换为 **Codex 原生、可恢复的会话**。本地 CLI，保留源数据归档、转换映射、损失报告和校验值，不执行历史命令或文件补丁。

**状态：实验性 Alpha，不是 TRAE 或 OpenAI 官方迁移工具。** 原生恢复已在 macOS、Codex CLI **0.154.0** 上用真实 app-server 验证；尚未验证 Codex 桌面端界面，也尚未打通本机新版 TRAE 加密数据库的自动提取。不能承诺任意版本、一键、100% 无损。

## 适用范围

| 输入/能力 | 当前状态 |
| --- | --- |
| 本项目结构化 JSON 格式 | 支持，包含完整的合成示例 |
| 明文 SQLite `chat_session` + `history_v2.messages.raw_messages` | 支持，启动时校验表结构；通过合成数据库/WAL 测试，尚无脱敏真实库端到端验收 |
| SQLCipher 4 数据库 | 可选读取接口，需要用户合法持有的 raw key 和 `sqlcipher3`；本机未验证实际解密 |
| 新版 TRAE 加密库，无密钥 | 只检测并报错，不扫描进程内存、不绕过保护 |
| Markdown 对话、旧版 `state.vscdb`、私有日志、压缩/分支快照 | 不做猜测解析，需先提供受支持的结构化导出 |
| macOS | 已运行本地测试 |
| Windows / Linux | 有路径发现、原生可执行文件解析和 CI 配置，未在本次本机实测 |

聊天、可见思考、命令、文件差异、MCP、动态工具可以成为原生条目。原生命令/差异需要**真实保存的 argv、退出码、changes、执行结果**。只有 `Read` / `Write` / Skill 文件读取等工具日志时保留动态工具记录，不凭名称编造补丁。Skill 定义、MCP 服务和授权不自动安装。

## 安装

需要 Python 3.9+。转换部分只有标准库依赖，验证/导入需要上述固定版本的 Codex。**包尚未发布到 PyPI**，先从源码安装：

```bash
git clone https://github.com/yororoA/TraeCode2Codex.git
cd TraeCode2Codex
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/trae2codex --help
```

Windows 使用 `py -m venv .venv`、`.venv\Scripts\python -m pip install .` 和 `.venv\Scripts\trae2codex`。下文命令假设已激活虚拟环境或通过 `pipx install .` 安装。

已有其他 Codex 版本时不要直接降级全局安装，可单独安装验证用版本：

```bash
npm install --prefix .local/codex @openai/codex@0.154.0
trae2codex doctor --codex .local/codex/node_modules/.bin/codex
```

Windows 可将 `--codex` 指向 npm 生成的 `codex.cmd`，工具会解析对应 `codex.exe`，不通过 shell 执行。无法解析时直接指定原生可执行文件。

## 先运行示例

示例只有合成数据，不会读取你的聊天：

```bash
trae2codex convert --source examples/session.json --cwd "$PWD" --output .local/demo
trae2codex verify .local/demo --codex .local/codex/node_modules/.bin/codex
```

`verify` 在临时 `CODEX_HOME` 和空项目中启动真实 Codex，依次检查：

1. 会话可以列出，首次 `resume` 能建立分页索引。
2. 原生条目数量、内容、顺序、工具关联和文件差异匹配。
3. 重启 app-server 后仍可读取并恢复。
4. 向本机回环模拟模型发送下一轮，历史消息、工具参数和结果按顺序进入请求。

验证不读取你的 Codex 登录信息，模型请求只发往 `127.0.0.1`，模拟模型只返回固定文本、不发起工具调用；没有真实付费模型调用。Codex 自身不是由本工具实现的网络沙箱。迁移包是明文敏感数据，校验值不是数字签名，请只验证可信来源。

## 迁移实际会话

先备份源数据。数据库副本须在关闭 TRAE 后复制，连同仍存在的 `-wal` / `-shm` 文件一起保存，不能只复制正在写入的主文件。

```bash
trae2codex doctor
trae2codex list --source /path/to/database.db
trae2codex plan --source /path/to/database.db --session SESSION_ID --cwd /absolute/project
trae2codex convert --source /path/to/database.db --session SESSION_ID --cwd /absolute/project --output /private/path/migration-bundle
```

`--session` 可重复；省略时选择全部有记录的会话。跨项目迁移建议分别执行，`--cwd` 会应用于本次所有会话；历史工具路径不会被重写，项目代码需自行保持同步。

累计快照必须显式加 `--history-mode snapshots`，默认使用增量记录。分叉、压缩导致前缀变化会报错，不擅自合并。缺少历史的会话列在 `skipped_sessions` 中。

加密数据库只支持用户合法持有的 **64 位十六进制 raw key**：

```bash
trae2codex list --source /path/to/database.db --key-env TRAE_DB_KEY
```

请在本地安全地设置 `TRAE_DB_KEY`，不要将密钥贴到聊天、命令参数、Issue 或仓库。`sqlcipher3` 是可选的外部依赖，需要适合操作系统的 SQLCipher 4 构建。工具不负责获取密钥；无合法密钥时必须先取得明文导出。

转换前发现疑似凭据会拒绝生成包。人工脱敏后重试，或者明确使用 `--accept-sensitive` 保留私有原文。扫描是启发式，零命中不代表没有敏感信息。

## 导入与回滚

先阅读 `report.json` 中的 `losses` / `warnings`，再验证和导入。**安装和回滚前关闭所有使用目标目录的 Codex 进程，并备份该目录。** 本工具的锁只协调迁移进程，不能阻止 Codex 并发写入。

```bash
trae2codex install /private/path/migration-bundle --codex-home /private/path/codex-test
trae2codex install /private/path/migration-bundle --codex-home /private/path/codex-test --codex /path/to/codex --apply
```

默认只预览；`--apply` 会先重新验证，通过后只添加原生 rollout 和迁移收据，不直接修改 Codex SQLite 或配置。存在损失时必须人工确认后加 `--allow-loss`。写入采用独占原子发布，同包重复执行不覆盖，安装中断后可用相同命令补齐。

首次对每条会话执行一次：

```bash
CODEX_HOME=/private/path/codex-test /path/to/codex resume THREAD_UUID
```

其中 UUID 由转换/安装输出。首次 `resume` 是必要步骤，用于建立分页条目索引。Windows PowerShell 先设置 `$env:CODEX_HOME`，再运行 `codex resume`。正式导入可显式指定 `--codex-home "$HOME/.codex"`；桌面端是否使用同一目录及兼容版本需单独确认。标题目前可能采用第一条用户消息，原 TRAE 标题保留在报告中。

撤回：

```bash
trae2codex rollback --codex-home /private/path/codex-test --bundle-id BUNDLE_SHA256
trae2codex rollback --codex-home /private/path/codex-test --bundle-id BUNDLE_SHA256 --apply
```

Bundle ID 在 `manifest.json` 中。仅删除收据登记且校验值未变的 rollout；继续对话或 Codex 改写文件后会拒绝回滚，避免删除新记录。不会清理 Codex 自己创建的索引缓存，列表可能需要刷新。源文件和迁移包始终保留。

## 明确不恢复的内容

- 未保存的内部推理、模型内部状态、审批授权、运行中的进程和原平台撤销检查点。
- 缺少源证据的逐步编辑、附件文件本体、子 Agent 分支关系和压缩前历史。
- 原系统/开发者指令不激活为 Codex 高优先级指令，仅留在归档并报告损失。
- 可见思考能以原生条目展示；供应商是否把无签名 reasoning 继续纳入模型上下文不保证。
- 超出目标模型上下文限制时验证可能拒绝，不自动截断。实际在线续聊会把历史发送给你配置的模型服务商，请先确认数据政策。

更多格式说明见 [源数据契约](docs/source-format.md)，工程验收与扩展见 [兼容性和开发](docs/development.md)。

## 开发验证

```bash
python3 -m unittest discover -s tests -v
TRAE2CODEX_TEST_CODEX=/path/to/codex python3 -m unittest discover -s tests -v
python3 -m pip install build
python3 -m build
```

Wheel 可供其他人通过 `pipx install dist/trae2codex-0.1.0-py3-none-any.whl` 安装。发布 PyPI、创建 Release 和实际上传均需维护者单独执行，本项目不会自动发布你的聊天或数据。
