# 源数据契约

`trae2codex.source.v1` 是本项目的可审计输入格式，**不是声称 TRAE 官方能够直接导出的格式**。其他导出器可实现这个契约。仓库的 `examples/session.json` 覆盖消息、可见思考、命令、文件变更、MCP 和动态工具。

## 最小输入

```json
{
  "format": "trae2codex.source.v1",
  "sessions": [
    {
      "session_id": "stable-source-id",
      "title": "Original title",
      "created_at": "2026-09-01T10:00:00Z",
      "cwd": "/absolute/project",
      "messages": [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi", "phase": "final_answer"}
      ]
    }
  ]
}
```

`session_id` 必须是非空字符串，作为稳定身份生成 UUID，不能用标题代替。时间支持带时区的 ISO 8601、Unix 秒或毫秒，不接受无时区字符串、NaN 或 Infinity。会话创建时间不能晚于第一条消息；消息顺序不能倒退。

消息可有 `timestamp` / `created_at`；没有时使用所属记录时间，记录没有时使用会话时间。这是时间精度降级，不代表每项操作发生于同一毫秒。`--cwd` 仅覆盖会话根路径，历史工具参数及 diff 路径保留原样。

## 数据库与多条历史记录

数据库适配器要求：

| 表 | 必须字段 |
| --- | --- |
| `chat_session` | `session_id`, `created_at` |
| `history_v2` | `id`, `session_id`, `messages`, `created_at` |

按 `created_at, id` 读取 `history_v2`。`messages` 必须是消息数组或含 `raw_messages` 的对象，也可为以上结构的 JSON 字符串。`history_v2_id` 存在时作为去重身份，内容冲突直接报错；不会全局按文本去重，用户重复提问不会消失。

JSON 可用 `records` 代替 `messages`：

```json
{
  "session_id": "stable-source-id",
  "created_at": "2026-09-01T10:00:00Z",
  "cwd": "/absolute/project",
  "history_mode": "increments",
  "records": [
    {
      "history_v2_id": "record-1",
      "created_at": "2026-09-01T10:00:00Z",
      "messages": {
        "raw_messages": [
          {"role": "user", "content": "Hello"},
          {"role": "assistant", "content": "Hi"}
        ]
      }
    }
  ]
}
```

`increments` 把记录视为顺序增量；`snapshots` 要求每条都是前一条的完整前缀扩展，只追加新后缀。分叉/压缩必须单独导出处理。命令行 `--history-mode` 可覆盖数据库或 JSON 的语义，覆盖记录会一并保存在归档。混合 Agent、分支或累计缓存不能未经核验就按增量导入。

## 内容块

| 输入 | 输出 |
| --- | --- |
| `user` / `assistant` 字符串或 `text` / `input_text` / `output_text` | 原生消息 + 模型历史 |
| `thinking.thinking`、`reasoning.text`、助手 `reasoning_content` | 保存的可见文本，原生 Reasoning |
| `tool_use` + `tool_result` | 原生工具条目 + 成对 function call/output |
| OpenAI 风格助手 `tool_calls[].function`、`role=tool` | 同上 |
| 未知块、图片/音频引用 | 标注为历史数据的文本 + 原文归档，明确报告损失，不联网下载 |
| `system` / `developer` | 只归档，不激活其指令，明确报告损失 |

助手 `phase` 仅接受 `commentary` / `final_answer` / 空值。其他值保留在归档。只有 `tool_result` 的 `role=user` 消息不是新轮次。工具结果与新用户文本混合的单条消息会拒绝解析，避免改变执行顺序。

工具调用基本格式：

```json
{"type":"tool_use","id":"call-1","name":"Read","input":{"file_path":"SKILL.md"}}
```

对应结果：

```json
{"type":"tool_result","tool_use_id":"call-1","content":"saved output","is_error":false,"duration_ms":12}
```

`is_error` 是可选布尔值，缺省 false。`duration_ms` 缺省表示未知，原生条目中保留空值，不补零。缺失结果会添加明确的“源记录缺失”标记并报告损失，不能伪造成功；部分命令/编辑降级为失败的历史动态工具。孤立结果以历史文本保留，不伪造调用。

## 原生工具证据

MCP：调用块提供 `server`、`tool`，或使用无歧义的 `mcp__server__tool` 名称。普通 TRAE MCP 名称不一定遵循该约定，不会自动猜测服务器。

命令：调用块额外提供 `kind: "command"`、非空字符串数组 `command`（原始 argv），可选绝对路径 `cwd`；结果块提供整数 `exit_code`，可选 `stdout` / `stderr`。没有退出码就拒绝原生命令映射。

文件变化：调用块提供 `kind: "file_change"` 和 `changes`：

```json
{
  "hello.txt": {"type": "add", "content": "hello\n"},
  "old.txt": {"type": "delete", "content": "old content\n"},
  "edit.txt": {
    "type": "update",
    "unified_diff": "@@ -1 +1 @@\n-before\n+after",
    "move_path": null
  }
}
```

结果块必须提供布尔 `success`。只有真实 patch、快照或导出器验证过的内容可填入 `changes`。转换器不读取目标项目来补差异，不执行补丁，不推断审批通过。

其他工具：默认 `DynamicToolCall`，完整保存名称、参数和结果。模型接口不合法的工具名会使用稳定别名，原名保留在原生条目和归档；不会因此安装新的可调用工具。

## 包结构与审计

```text
bundle/
  manifest.json
  report.json
  source/<thread-uuid>.json
  sessions/YYYY/MM/DD/rollout-<timestamp>-<thread-uuid>.jsonl
```

源归档保留所选会话记录，而不是整库备份：其他数据库表、附件本体、数据库运行状态不包含在内。报告中的 `mappings` 用 `source_ref` 对应输出 ordinal 区间，`source_counts` 区分实际结果与合成缺失标记，`losses` / `warnings` 说明所有已识别降级。

文件 SHA-256 和 manifest digest 用于完整性与重复导入判断，不证明包来自可信作者。验证/安装会从归档重新生成 rollout，拒绝无法复现的任意会话事件。格式版本暂不做跨版本迁移，工具升级后请保留原始输入和旧包。
