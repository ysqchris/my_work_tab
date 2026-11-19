---
name: workbench-operations
description: 通过个人工作台 HTTP API 查询和维护待办、工作总结、笔记、书签和知识库，或查看 KM 好文、学堂直播和 IMate 运营数据。用户用自然语言要求操作「我的工作台」时使用。
---

# 我的工作台

把用户的自然语言请求转换成已有 API 调用。比如「明天跟进灰度时间，优先级高」「把刚才那条待办标完成」「根据昨天的工作总结列出要跟进的事项」。

## 连接与鉴权

- 使用用户或运行环境提供的 `WORKBENCH_BASE_URL`，例如工作台的 HTTPS 地址或连接工作台的本机 SSH 转发地址。这个工作台的腾讯云部署曾使用 `http://193.112.129.14/`，属于明文 HTTP。发送令牌或读取私有数据前，使用已验证的 HTTPS 或加密隧道。前端调试代理 `127.0.0.1:8766` 的上游也是这个 HTTP 地址。
- 使用服务端 `API_TOKEN` 对应的客户端秘密 `WORKBENCH_API_TOKEN`，通过 `Authorization: Bearer <token>` 发送。令牌从运行环境或秘密管理器取用。凭据放在技能文件之外，调用时避免将其打印到日志、对话或工具输出。具备已登录浏览器会话时，也可以在同源页面使用会话鉴权。
- 用 `GET /healthz` 检查连接，用 `GET /api/tasks` 检查 API 访问权限。`GET /api/auth/status` 的 `logged_in` 只表示浏览器会话状态，不能用于判断 Bearer 令牌有效性。遇到 401 时停止写入并核对凭据或目标地址。AI 环境需要能发 HTTP 请求或使用有权限的终端。
- JSON 请求设置 `Content-Type: application/json`。核对 HTTP 状态、返回 JSON 和预期字段。携带认证头时关闭自动跳转，确保凭据直接发给已配置的工作台。

## 待办

| 意图 | 请求 | 数据 |
| --- | --- | --- |
| 查看待办 | `GET /api/tasks` | 从 `items` 读取，按 `done`、`date` 和 `priority` 筛选 |
| 新增 | `POST /api/tasks` | `title`，可选 `priority`、`date`、`content`。返回 201 和创建后的条目 |
| 编辑或完成 | `PATCH /api/tasks/{id}` | 仅发送修改的字段，例如 `{"done":true}`。恢复为未完成用 `false` |
| 删除 | `DELETE /api/tasks/{id}` | 成功返回 `{"ok":true}` |

- `title` 用简洁的行动描述。优先级取 `高`、`中`、`低`，创建时未指定则采用界面默认值 `中`。将「明天」「下周一」按用户时区换成 `YYYY-MM-DD`，这个工作台默认按 Asia/Shanghai 理解日期。
- 接口省略 `date` 时会填服务器当天日期。用户没给日期且希望留空时，创建或编辑时显式传 `"date":""`。
- 按标题操作已有待办前，从列表找真实 ID。命中多条时请用户选定。用户明确说「添加」且字段足以执行时直接创建。用户说「从总结里提取」「帮我看看哪些要跟进」时先给候选及原文依据，匹配已有待办并标出可能重复的项目，待用户明确要求写入后执行。
- 时间和含义不明时，先补齐影响结果的信息。例如「明早 9 点提醒我」里的具体提醒时间，当前接口只有日期字段，回复应说明待办日期与定时提醒的区别。

示例。「帮我加个高优先级待办，跟进灰度时间，日期先留空。」

```http
POST /api/tasks
Content-Type: application/json

{"title":"跟进灰度时间","priority":"高","date":"","done":false}
```

## 工作总结与其他资料

| 数据 | 读取 | 写入 |
| --- | --- | --- |
| 工作总结 | `GET /api/daily_brief`，或 `GET /api/daily_brief?date=YYYY-MM-DD` | `POST /api/daily_brief`，JSON 含 `date`、完整 Markdown `content`，可选 `source`。同日覆盖 |
| 笔记 | `GET /api/notes` | `POST /api/notes`，字段 `title`、`content`、可选 `topic` |
| 书签 | `GET /api/bookmarks`；可用 `GET /api/bookmark-groups` 读取真实分组 | `POST /api/bookmarks`，字段 `title`、`url`、`kind`、`group`，可选 `summary`。类型用 `memo` 或 `frequent`，无分组要求时可用 `external` |
| 知识库 | `GET /api/knowledge` | `POST /api/knowledge`。文档用 `kind:"document"`、`title`、`content`，可选 `topic`。链接用 `kind:"link"`、`title`、`url` |
| KM 好文、学堂直播、IMate 运营数据 | 分别使用 `GET /api/km-hot`、`GET /api/qlearning-lives`、`GET /api/imate_ops` | 本技能用现有缓存回答查询 |

- `notes`、`bookmarks`、`knowledge` 的列表返回 `items`。修改用 `PATCH /api/{资源}/{id}`，删除用 `DELETE /api/{资源}/{id}`。删除知识库附件条目也会删除对应文件。URL 用用户提供或已验证的值。写入文本内容时使用普通文本，保留笔记或任务原有富文本时先读取再合并。
- 读取指定日期的总结时总是传 `date`。省略日期可能返回以前的总结，需检查 `is_fallback` 和实际日期。追加或局部修改先读同日内容，合并后一次提交完整内容。覆盖整份已有总结前确认用户有这个意图。
- KM、直播和运营数据以响应里的日期及更新时间为准，缓存为空时说明。用户的「看看最新」本身只触发查询。

## 执行结果

写入前检查目标实例和目标条目。删除、整份覆盖或批量变更时先对齐具体范围。逐项写入，避免对同一个集合并发修改。创建请求超时或结果不明确时，先查列表核对是否已写入，避免重发产生重复条目。返回简短的结果，包含实际标题、日期和必要的 ID。只有收到成功响应，或通过回读确认结果后，才报告成功。服务的 `/api/ai/chat` 现有工具仅查询，实际写入使用上述资源接口。
