# astrbot_plugin_jev

<div align="center">

[![Version](https://img.shields.io/badge/version-V1.3.0.jev.1-blue.svg)](CHANGELOG.md)
[![AstrBot](https://img.shields.io/badge/AstrBot-%E2%89%A5v4.11.0-green.svg)](https://github.com/AstrBotDevs/AstrBot)
[![License](https://img.shields.io/badge/license-AGPL--3.0-orange.svg)](LICENSE)
[![Visualizer](https://img.shields.io/badge/%F0%9F%8E%AE-%E5%86%B3%E7%AD%96%E6%B5%81%E7%A8%8B%E6%A8%A1%E6%8B%9F%E5%99%A8-66CCFF.svg)](https://jev-decision-flow.pages.dev)

**让 Bot 学会「看场合说话」的群聊增强插件** —— 以 AI 读空气为核心，叠加 System One（Jev 式）极速决策模型复核

本仓库是 [Him666233/astrbot_plugin_group_chat_plus](https://github.com/Him666233/astrbot_plugin_group_chat_plus)（V1.2.3.hotfix.2）的 fork，
在其完整功能之上集成了**阿里云百炼 System One 决策模型**。AGPL-3.0 协议沿用原作，感谢原作者 [@Him666233](https://github.com/Him666233)。

</div>

---

## 这个 fork 多了一层什么

原版判断「要不要插话」靠纯随机概率门（概率对了就说话，不对就沉默），它不知道群里正在聊什么。本 fork 在概率门之后追加一次 **50~70ms 的决策模型判断**：

```
群消息 → 原版概率门（原样保留）→ 🧠 System One 复核「值得插话吗」→ 读空气 AI → 回复
                                        │
                                        ├─ join P(yes) ≥ 阈值 → 放行，并建议插话方式（提问/分享/附和）
                                        ├─ P < 阈值 → 保持安静（不占额度）
                                        └─ 超时/出错 → 「未决策」降级回原版逻辑，绝不哑掉
```

| 能力 | 说明 |
|---|---|
| 三种介入模式 | `off`（默认，行为与原版逐字节一致）/ `hybrid`（概率门通过后复核，推荐）/ `replace`（替代概率门） |
| 决策内容 | join 概率（noul）、话题相关度（score）、建议插话方式（choice） |
| 每群独立限频 | 每小时上限、两次插话最小间隔、Bot 冷却、短消息跳过——全部按群计数，插话与主动开场分桶 |
| 全链路降级 | 超时 / 网络错误 / 坏响应 / 任何异常都返回「未决策」，回落原版行为 |
| 每群覆盖配置 | JSON 文件按群覆盖阈值/模式/开关，约 30 秒热生效，无需重启 |
| 主动开场复核 | 沉默开话题链路同样接入决策（独立额度） |
| WebUI | 配置流程图新增「🧠 System One 决策 / 开场复核」节点（1451 面板） |
| 可视化 | 交互式决策流程模拟器：https://jev-decision-flow.pages.dev |

## 🚀 快速开始

### 安装

AstrBot 插件市场添加仓库地址 `https://github.com/czyczy23/astrbot_plugin_jev`，或手动 clone 到 `data/plugins/`。

> 已在跑原版 Chat_PLUS 的注意：插件 ID 未改（`astrbot_plugin_group_chat_plus`），本 fork 可直接替换原目录，**原有配置与数据全部延续**，`jev_mode` 默认 `off` 保证行为零变化。

### 前置条件

- 阿里云百炼（DashScope）账号，开通 **TokenPlan 决策模型** 服务
- 一个 `sk-` 开头的 API Key（[百炼控制台](https://bailian.console.aliyun.com/) 创建）

### 三步启用

| 步骤 | 配置项 | 填什么 |
|---|---|---|
| 1️⃣ | `enable_systemone_decision` | `true`（总开关） |
| 2️⃣ | `systemone_api_key` | 你的 `sk-` Key（仅存于本插件配置，不进任何代码仓库） |
| 3️⃣ | `jev_mode` | `hybrid`（推荐：保留你调好的概率节奏，决策模型负责把关质量） |

重启插件后看日志确认：

```
🧠 [SystemOne] 决策模块已启用：model=decision-model-preview, endpoint=https://token-plan..., jev_mode=hybrid, 阈值=0.60, ...
```

之后群里出现插话决策时（`jev_log_enabled` 默认开）：

```
🧠 [SystemOne] 群123456789 插话判断: 通过 join=0.66 (阈值0.60) | 相关度=2.00 方式=react | 延迟=56ms
```

## ⚙️ System One 接口与模型（可自定义）

**接入端点和模型名都是配置项，不需要改任何代码。** 留空即用默认值：

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `systemone_base_url` | `https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/systemone` | System One 协议端点。可指向自建网关/代理（内网转发、中转鉴权等），要求转发原始请求/响应体 |
| `systemone_model` | `decision-model-preview` | 决策模型名。可换成百炼 TokenPlan 上任何 System One 协议兼容的决策模型 |
| `systemone_api_key` | （空） | `sk-` Key，随 `Authorization: Bearer` 头发出 |
| `systemone_timeout` | `4.0` | 单次请求超时（秒），实测端到端约 250ms，一般无需调大 |

```jsonc
// 插件配置中自定义网关 + 新模型的示例
{
  "systemone_base_url": "https://my-gateway.internal/v1/systemone",
  "systemone_model": "decision-model-v2",
  "systemone_timeout": 3.0
}
```

**两处注意**：

1. System One 是专用协议（`POST` 一组 `state` + `questions`，返回概率/评分），**不是** OpenAI `chat/completions`——普通聊天模型地址填进去只会得到解析失败然后降级，不会崩。
2. 改完后重启插件，启动日志会打印当前生效的 `model` 与 `endpoint`，看一眼就知道配置有没有吃进去。

## 🛡️ 决策行为与限频

### jev_mode 三模式

| 模式 | 行为 | 适用 |
|---|---|---|
| `off` | 完全不介入（默认） | 升级后先观察兼容性 |
| `hybrid` | 概率门通过后再复核；未决策时视为概率门已过 | 推荐：插话频率 ≈ 原频率 × 判定通过率 |
| `replace` | 直接用 join P≥阈值替代概率门；未决策时回落旧概率门 | 想让模型全权定节奏 |

### 四道每群闸门（全部本地零延迟，不占 API 额度）

| 闸门 | 配置项 | 默认 |
|---|---|---|
| 每小时插话上限 | `jev_max_joins_per_hour` | 8（0=不限） |
| 两次插话最小间隔 | `jev_min_join_interval_sec` | 120s |
| Bot 冷却（刚说话后） | `jev_bot_cooldown_sec` | 90s |
| 消息最短长度 | `jev_min_message_length` | 6 字 |

### 降级矩阵（任何失败都不影响可用性）

| 异常 | 结果 |
|---|---|
| 超时 / HTTP 错误 / 断网 / 坏 JSON / 缺 join 字段 | 「未决策」→ replace 走旧随机门，hybrid 视为概率门已过 |
| 模块内部任何未预期异常 | 同上（整体 try/except 兜底） |
| API Key 留空 | 模块自我禁用，日志提示一句，零开销 |
| 消息短于阈值 | 跳过决策（不浪费调用） |

### 每群覆盖（可选）

`plugin_data/astrbot_plugin_group_chat_plus/systemone_group_config.json`：

```json
{
    "123456789": { "join_threshold": 0.75, "max_joins_per_hour": 4, "jev_mode": "hybrid" },
    "987654321": { "enable": false }
}
```

未列出的群走全局默认；文件按 mtime 约 30 秒热重载，写坏只回退全局默认并记 warning。

## 🖥️ WebUI 与可视化

- **配置面板**（默认 `http://<host>:1451`）：科技树式消息流程图，System One 节点（🧠）位于「概率判定系统」阶段，点开即改 21 项配置，保存走 schema 校验，重启插件生效
- **决策流程模拟器**：https://jev-decision-flow.pages.dev —— 群聊剧本回放 + 决策链逐节点追踪，可对比三模式/阈值/故障降级行为

## 🧪 开发与测试

```bash
python3 -m unittest tests.test_systemone_judge   # 67 个用例，全离线（mock HTTP 与时钟）
```

- 设计文档（接入点/数据流/降级矩阵/回滚）：[docs/JEV_DESIGN.md](docs/JEV_DESIGN.md)
- 原版完整文档：[docs/ORIGINAL_README.md](docs/ORIGINAL_README.md)
- 更新历史：[CHANGELOG.md](CHANGELOG.md)

## 🙏 致谢与协议

- 本 fork 基于并感谢 [Him666233/astrbot_plugin_group_chat_plus](https://github.com/Him666233/astrbot_plugin_group_chat_plus)（V1.2.3.hotfix.2）
- [AstrBot](https://github.com/AstrBotDevs/AstrBot) 项目
- 协议：**AGPL-3.0**（沿用原作，见 [LICENSE](LICENSE)）
