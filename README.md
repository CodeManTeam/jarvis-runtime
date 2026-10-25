# Jarvis — 可审计的个人助理运行时

一个自托管的个人助理服务：语音进、行动出，**每一次行动都可追溯，任何高风险动作都要你逐次许可**。
它同时是一个**可扩展的能力框架**：加一种能力 = 往 `app/capabilities/` 里加一个文件。

```text
手表 / 中控台 / 任意 HTTP 客户端
        │  HTTP + WebSocket + SSE
        ▼
   FastAPI 运行时（本仓库）
   ├─ 决策与工具循环（模型选工具，代码只负责校验、存储、执行）
   ├─ 能力包（pptx / pdf / 静态页 / 读网页 / 跑代码 / 读上传文件）
   ├─ 长任务（Job + 工作区 + 轨迹落盘 + 可取消 + 产物交付）
   ├─ 许可矩阵（逐次 / 一次性 / 自动 / 禁止，作用域哈希，用一次即作废）
   └─ 事件总线（SSE 实时推送到中控与手表）
```

## 这个项目想解决什么

大多数"个人助理"项目的失败不是能力不够，而是**不可信**：它会把没做的事说成做完了，
会把一次授权当成永久授权，会在你不知道的时候动你的东西。本项目的三条硬约束：

1. **失败即失败**：额度不足、鉴权失败、超时、找不到程序，各有各的原话，
   不包装成"暂时无法完成，请稍后再试"。
2. **授权不复用**：高风险动作（发消息、跑命令、操作设备）逐次许可，许可绑定动作内容哈希，
   用一次即作废；`deny` 档位不是"超级权限"，而是拒绝一切。
3. **结论要有依据**：每个结论可以展开到四层证据（原始输入 → 判断 → 工具调用 → 结果）。

## 快速开始

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env             # 填入你自己的模型端点与 DEVICE_TOKEN
uvicorn app.main:app --reload --port 8787
```

- 中控台：<http://127.0.0.1:8787/console/>
- API 文档：<http://127.0.0.1:8787/docs>
- 健康检查：`curl http://127.0.0.1:8787/health`

任何 **OpenAI 兼容** 的推理端点都可以用（`DEEPSEEK_*` 系列环境变量），
不绑定特定厂商；`LLM_API_FORMAT` 支持 `responses` 与 `chat_completions` 两种线格式。

## 它能做什么

| 能力 | 说明 | 默认档位 |
| --- | --- | --- |
| `artifact.create_pptx` | 生成真实的 .pptx 并给出链接 | 自动 |
| `artifact.create_pdf` | 生成 PDF（内置 CJK 字体，无需系统字体） | 自动 |
| `artifact.create_web_page` | 生成静态页并托管 | 自动 |
| `web.fetch_url` | 读网页正文（含 SSRF 防护） | 自动 |
| `code.run_python` | 写并运行一段 Python | **逐次许可** |
| `document.*` | 读取用户上传的 PDF / Word / Markdown / 文本 | 自动 |
| `job.*` | 长任务：多步骤、工作区、可恢复、产物交付 | 创建后需批准 |

加能力的方式：

```python
# app/capabilities/my_thing.py
from .base import CapabilityPack

PACK = CapabilityPack(
    name="artifact.create_mindmap",
    description="把要点生成一张思维导图",
    input_schema={...},
    handler=my_handler,
    capability="artifact.create",   # 权限矩阵条目自动注册
    produces="html",
    delivery=["把链接给用户", "问要不要改主题"],
)
```

不需要改 service、不需要改提示词、不需要改权限表——注册表会把工具定义、权限条目、
以及模型看到的能力菜单全部推导出来。

## 长任务（Job）

一件需要几十步才能完成的事（比如"做一个三文件的小站并跑起来验证"）不再是做不到，
而是变成一个 Job：

- 有**独立工作区** `data/jobs/<id>/workspace`，文件跨步骤累积；
- **每一步都落盘**（`trajectory.json`），进程重启可恢复；
- 创建后处于 `queued`，**批准后才允许执行命令**（`job.run_shell` 授权，作用域锁在该 Job）；
- 产物自动进入交付链：拿到短码和链接，而不是烂在工作区里。

## 目录结构

```
app/             FastAPI 运行时、决策、工具、Job、能力包
console/         浏览器中控台（无框架、无构建）
watch-client/    Wear OS 手表客户端（Java，360×480）
docs/            设计与决策文档（含 UX 原则、权限模型、能力框架）
tests/           pytest 测试（约 370 条）
deploy/          systemd / nginx / Caddy 示例配置
```

## 部署

`deploy/` 下是示例配置：`jarvis.service`（systemd）、`nginx.example.conf`、`Caddyfile.example`。
所有主机名与路径都是占位符，按需替换。

```bash
# 关键环境变量
PUBLIC_BASE_URL=https://your-jarvis-host   # 生成链接时使用
DEVICE_TOKEN=<长随机串>                     # 设备与中控台鉴权
```

## 测试

```bash
python -m pytest -q
```

手表端：

```bash
cd watch-client
./gradlew assembleDebug
```

## 已知限制

- **真实手表的交互未验证**：仓库里有完整的 UI 与逻辑，但没有在 360×480 真机上做触摸、
  计时可见性、语音时序的验收。
- **没有沙箱**：`code.run_python` 与 Job 的 shell 在服务进程同权限下执行，
  靠"逐次许可"控制风险，不是靠隔离。部署到多租户环境前必须自行加沙箱。
- 能力数量刻意保持很少：宁可少而可靠，不要多而不可信。

## 许可

MIT，见 [LICENSE](LICENSE)。安全问题请见 [SECURITY.md](SECURITY.md)。
