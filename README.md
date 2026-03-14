# 🛡️ AiModerater - AstrBot AI审核插件

基于大模型的群消息AI审核插件，支持文本/图片审核、多API故障转移、可配置处罚链。

## ✨ 功能特性

- **实时群消息审核** - 自动拦截并审核群成员发送的文本/图片消息
- **多模态审核** - 支持视觉模型识别图片内容，或OCR提取文字后审核
- **多API故障转移** - 配置多个大模型API，主接口不可用自动切换
- **可配置处罚链** - 根据违规次数递进处罚：警告→禁言→踢出→拉黑
- **正则预过滤** - 简单规则先行过滤，减少API调用成本
- **违规记录** - SQLite持久化存储，支持按用户/时间/类型查询
- **通知推送** - 违规信息推送到指定群/人，支持自定义消息模板
- **白名单机制** - 管理员/指定用户跳过审核
- **临时暂停** - 支持临时暂停审核，定时自动恢复
- **频率控制** - 可配置每分钟最大API调用量

## 📦 安装

将插件目录放入 AstrBot 的 `data/plugins/` 目录：

```bash
cd AstrBot/data/plugins
git clone https://github.com/sweepikun/AiModerater.git astrbot_plugin_ai_moderator
```

然后重启 AstrBot 或在 WebUI 插件管理页面点击 **重载插件**。

## ⚙️ 配置

在 AstrBot WebUI 的插件配置页面进行可视化配置：

### 基础配置

| 配置项 | 说明 |
|--------|------|
| 启用审核的群号列表 | 填入需要启用审核的群号 |
| 群组独立设置 | 按群配置审核类型（文本/图片） |
| 正则预过滤规则 | 每行一条正则，命中直接判定违规 |

### API配置

支持配置多个大模型API实现故障转移：

| 字段 | 说明 |
|------|------|
| API名称 | 自定义名称，用于日志显示 |
| API Key | 大模型API密钥 |
| Base URL | API基础地址 |
| 文本审核模型 | 用于文本审核的模型名 |
| 视觉审核模型 | 用于图片审核的模型名（可选） |

### 处罚配置

处罚层级链示例：`["warn", "mute_600", "mute_3600", "kick"]`

| 处罚类型 | 格式 | 说明 |
|----------|------|------|
| 警告 | `warn` | 发送警告消息 |
| 禁言 | `mute_秒数` | 禁言指定秒数 |
| 踢出 | `kick` | 移出群聊 |
| 拉黑 | `ban` | 永久拉黑 |

### 通知配置

支持自定义通知模板，可用变量：

`{user}`, `{user_id}`, `{group}`, `{group_id}`, `{content}`, `{reason}`, `{category}`, `{confidence}`, `{time}`, `{count}`, `{punishment}`

## 🎮 命令列表

```
/审核 pause [分钟]       暂停审核（可指定分钟数）
/审核 resume            恢复审核
/审核 status            查看审核状态和API健康状态
/审核 query [用户ID] [日期]  查询违规记录
/审核 stats [群号]       查看统计信息
/审核 whitelist add|remove|list [用户ID]  白名单管理
/审核 test <文本>        测试审核某文本
/审核 cleanup [天数]     清理过期违规记录
```

## 📁 项目结构

```
astrbot_plugin_ai_moderator/
├── main.py              # 插件主入口
├── _conf_schema.json    # WebUI可视化配置Schema
├── metadata.yaml        # 插件元数据
├── requirements.txt     # Python依赖
└── lib/
    ├── __init__.py
    ├── models.py        # 数据模型定义
    ├── db.py            # SQLite数据库操作
    ├── llm_client.py    # 多API调用 + 故障转移
    └── moderator.py     # 审核核心逻辑
```

## 📄 License

MIT
