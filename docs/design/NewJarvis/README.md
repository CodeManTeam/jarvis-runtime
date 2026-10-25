# NewJarvis 设计基线（v1.0 草案）

这组文档是 v1.0 大版本的设计与实现基线，**取代**此前散落在 `docs/` 里的浅色版手表
设计规范和"以日志为中心"的中控形态。设计源是 Figma 文件 `UPeWdeWBAu3zupEJ3hmPDS`
的 Page 2，共 14 帧，已完整提取到 [figma/](figma/README.md)。

## 阅读顺序

| # | 文档 | 回答什么问题 | 状态 |
| --- | --- | --- | --- |
| 1 | [V1_SPEC.md](V1_SPEC.md) | 这个大版本要做什么、不做什么、怎么算完成 | 草案 |
| 2 | [DESIGN_SYSTEM_V2.md](DESIGN_SYSTEM_V2.md) | 颜色、字号、间距、圆角、缩放与组件规格 | 草案 |
| 3 | [WATCH_UI.md](WATCH_UI.md) | 手表 9 个状态屏的结构、文案与数据绑定 | 草案 |
| 4 | [CONSOLE_UI.md](CONSOLE_UI.md) | 中控 5 个页面的结构、表格与动作 | 草案 |
| 5 | [PERMISSION_MODEL.md](PERMISSION_MODEL.md) | 四档门控、许可作用域、受信 Agent、远程配置 | 草案 |
| 6 | [MEMORY_MODEL.md](MEMORY_MODEL.md) | 记忆分层、候选、修订、忘记与销毁 | 草案 |
| 7 | [ACTIVITY_EVIDENCE.md](ACTIVITY_EVIDENCE.md) | 注意力分类、四层依据、置信度、降级 | 草案 |
| 8 | [DEVICE_OPS.md](DEVICE_OPS.md) | 指标、路由降级、远程配置、升级回滚、RCE | 草案 |
| 9 | [PRODUCTION_EVIDENCE.md](PRODUCTION_EVIDENCE.md) | 旧生产环境到底被怎么用（1611 事件、407 忽略、12 任务） | 已完成 |
| 10 | [AGENT_FRAMEWORK.md](AGENT_FRAMEWORK.md) | v2 后端骨架：Thread / Run / Agent / Skill / Schedule | 设计 |
| 9 | [DECISIONS.md](DECISIONS.md) | 已拍板的决策与对两个实现 CLI 的约束 | 生效中 |
| — | [DESIGN_COMPARISON.md](DESIGN_COMPARISON.md) | 草图与当前实现的逐项差距分析（背景材料） | 已完成 |

## 与旧文档的关系

| 旧文档 | 处置 |
| --- | --- |
| [DESIGN_SYSTEM.md](../../DESIGN_SYSTEM.md) | **被 [DESIGN_SYSTEM_V2.md](DESIGN_SYSTEM_V2.md) 取代**：浅色 `#F7F5F0` 不再是基线，改为深色 `#0B0E11` |
| [UI_LAYOUT_PLAN.md](../../UI_LAYOUT_PLAN.md) | **被 [WATCH_UI.md](WATCH_UI.md) 取代**：首页从"配置卡 + 单主卡 + 时间线"改为"注意力流" |
| [PRODUCT_SPEC.md](../../PRODUCT_SPEC.md) | 保留定位与边界，但"中控台只做查看与调试"的描述作废 |
| [GATING_MEMORY_ARCHITECTURE.md](../../GATING_MEMORY_ARCHITECTURE.md) | 保留主链路思想，具体模型以 [PERMISSION_MODEL.md](PERMISSION_MODEL.md) 与 [MEMORY_MODEL.md](MEMORY_MODEL.md) 为准 |
| [ACTIVITY_CONTRACT.md](../../ACTIVITY_CONTRACT.md) | 保留现有契约，由 [ACTIVITY_EVIDENCE.md](ACTIVITY_EVIDENCE.md) 追加字段 |
| [ROADMAP.md](../../../ROADMAP.md) | v1.0 开工时需新增一节；本文档组是它的输入，不是替代 |

## 状态标记约定

本文档组统一使用三种标记，避免把"画了界面"当成"实现了功能"：

- **已实现**：当前 `app/` 或 `watch-client/` 已有代码，并注明文件。
- **待实现**：设计确定，代码未写。
- **仅设计**：只在草图里存在，能力本身尚未设计完，不能对外承诺。

## 可复现性

所有数值（颜色、字号、圆角、坐标）都来自 Figma 源文件而非目测，可随时重拉：

```powershell
python scripts\figma_pull.py --key UPeWdeWBAu3zupEJ3hmPDS --page "Page 2"
```

对应产物：`figma/texts.md`（670 条文案与帧内坐标）、`figma/tokens.md`（色值/字号/圆角统计）、
`figma/frames.json`（1329 个节点的完整几何）。
