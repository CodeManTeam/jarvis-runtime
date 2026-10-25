# 能力框架（Capability Framework）

- 状态：**设计**，本文件是实现的唯一契约
- 目标：**加一种能力 = 加一个模块**，不改 service、不改提示词、不改权限表
- 背景：用户明确要的是"可拓展的能力框架"，不是一个又一个硬编码功能

---

## 0. 问题定位

当前实现（截至 `6e2cc4c` 之后）里，pptx 能力是这样加进去的：

1. 在 `service._build_tool_registry` 里手写一个 `ToolDefinition`
2. 在 `policy.CAPABILITY_MATRIX` 里手写一行权限
3. 在提示词里手写一段"你能做什么"
4. 在测试里手写一份预期

**四处手写、四处会漂移。** 加第三种能力（PDF、图表、视频）要重复四次，忘一处就出现
"提示词说有、工具其实没有"——这正是生产上它回答"我能把它跑起来"的原因。

## 1. 调研：这行成熟做法的三个借鉴点

| 来源 | 做法 | 我们取什么 |
| --- | --- | --- |
| [MCP 工具规范](https://modelcontextprotocol.io/specification/2026-07-28/server/tools.md) | 工具以 `name` / `description` / `inputSchema` 自描述，服务端统一暴露 | **声明式**：能力自己描述自己，运行时不认识具体能力 |
| [Claude Code 扩展机制](https://code.claude.com/docs/en/features-overview) | skills 用渐进披露：先给短清单，细节按需加载 | **渐进披露**：提示词里只放能力菜单，不放开全部 schema |
| [能力声明的安装同意流](https://github.com/NousResearch/hermes-agent/issues/64228) | 能力升级/扩权需要重新征得同意 | **变更需再同意**：包声明权限，但扩大权限必须用户确认 |

一句话：**声明式 + 渐进披露 + 变更同意**。

## 2. 四项设计决定

### D1 · 能力包是模块，不是注册代码

```
app/capabilities/
  base.py              # 契约与注册表（已存在）
  artifact_pptx.py     # PACK = CapabilityPack(...)
  web_page.py          # PACK = ...
  <新能力>.py           # 加文件即加能力
```

包只声明，不关心自己被谁调用。

### D2 · 提示词只放菜单

提示词里出现的不是全部 JSON Schema，而是：

```
你可以调用以下能力（真实清单，不在其中就是没有）：
- artifact.create_pptx — 生成 .pptx 并返回链接（产出：pptx）
- artifact.create_web_page — 生成静态网页并托管（产出：html）
```

完整 schema 仍随工具一起给模型（现有机制已如此），但**能力自述**只用菜单，
避免每加一个能力就撑大系统提示。

### D3 · 产出物是一等公民

能力可以声明 `produces` 与 `delivery`：

```python
produces="pptx",
delivery=[
    "把链接直接给用户，并确认能不能打开",
    "问要不要改页数、补一页摘要或换顺序",
    "问是否需要别的格式（PDF / 网页版）",
]
```

运行时装成"交付提示"，注入到决策上下文。**这是"做完还能继续谈交付"的机制**，
不是靠模型即兴发挥。

### D4 · 权限由包声明，但包不能自我解锁

- 包声明 `capability` / `level` / `levels` / `scope`，注册时自动进入矩阵；
- 已有条目**不被覆盖**（手工表优先）；
- `LOCKED_CAPABILITIES` **只能手写**，包不得声明自己 locked；
- 包把 `level` 从 `auto` 放宽到 `once`/`per_action` 属于扩权，需要写进变更记录并由用户确认
  （v1 先做到"注册时记录来源"，同意流下一步再做）。

## 3. 契约

```python
@dataclass
class CapabilityPack:
    name: str                        # 模型看到的工具名
    description: str                 # 一句话，进菜单
    input_schema: dict               # JSON Schema
    handler: Callable[[dict, Any], Any]   # (args, service) -> dict
    capability: str | None = None    # 权限键，如 "artifact.create"
    level: str = "auto"
    levels: tuple[str, ...] = ("auto", "once", "deny")
    scope: str = "none"
    timeout_seconds: float = 30.0
    produces: str | None = None      # 产出物类型，用于交付提示
    delivery: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
```

**handler 约定**

- 入参已由工具运行时校验过 schema；
- 返回 dict；成功必须包含可交付信息（如 `path` / `url`）；
- 失败**返回** `{"ok": False, "error": ..., "kind": ...}` 或抛 `ArtifactError`，
  由框架转成"这次没有完成"，**不得抛通用异常冒充成功**。

## 4. 运行流程

```
import app.capabilities            →  发现 app/capabilities/*.py
  └─ PACK/PACKS                    →  注册进 REGISTRY
       ├─ register_capability()    →  权限矩阵自动补条目（不覆盖手工项）
       ├─ tool(service)            →  转成 ToolDefinition 进 ToolRegistry
       └─ describe()               →  能力菜单 + 交付提示

一次调用：
  模型选工具 → ToolRuntime 校验 schema → handler(args, service)
            → 产出物落盘 + 入库 → 返回 {url, bytes, ...}
            → 交付提示进入下一轮，模型据此问"怎么给你"
```

## 5. 失败语义（沿用已定的红线）

| 情况 | 表现 |
| --- | --- |
| 缺依赖（如没装 python-pptx） | `ArtifactUnavailable` → "服务端没有安装 pptx 生成库" |
| 入参不合法（空标题/空页） | `ArtifactError` → 明确说缺什么 |
| 写盘失败 | `ArtifactUnavailable` + 异常类型名 |
| 能力不存在 | 模型不得声称有；菜单里没有就是没有 |

## 6. 产出物与交付

- 落盘目录：`data/artifacts`（可通过 `JarvisService(artifacts_root=...)` 注入，测试用临时目录）
- 公网路径：`/files/<id>-<name>.<ext>`（nginx 已把整站反代到 8787）
- **安全**：`/files` 必须带 `Content-Security-Policy: sandbox`，让用户产出的 HTML
  不能读取同源的 console token。
- 清单位于 `GET /api/artifacts`。

## 7. 验收标准

1. `pytest` 全绿，且包含：发现、权限自动注册、能力→工具、pptx 真实产出（zip 校验）、
   静态页产出、失败语义、交付提示非空。
2. 新增一个能力**只增加一个文件**（测试用一个临时能力包证明）。
3. 提示词里的能力清单来自注册表——删掉一个包，清单自动少一条。
4. `/files` 响应带 CSP sandbox。

## 8. 迁移清单

| 现有工具 | 迁移成包 |
| --- | --- |
| `system.escalate` | `capabilities/escalate.py` |
| `device.*` / `task.list` / `memory.search` / `location.*` / `event.*` / `calendar.*` | 先留在 service（它们是系统查询，不是"能力"），但**必须出现在菜单里** |

## 9. 不做

- 不做远程能力市场（先把本地声明做对）。
- 不做包的动态热加载（重启生效即可，避免半加载状态）。
- 不做能力版本协商（v1 用 name 唯一，改名即新能力）。

---

## 10. 现有能力清单

加能力的成本已经降到"一个文件"，所以清单要能被随时核对：

| 能力 | 权限档 | 产出 | 说明 |
| --- | --- | --- | --- |
| `artifact.create_pptx` | auto | pptx | python-pptx，封面 + 小节 |
| `artifact.create_pdf` | auto | pdf | reportlab + CID 字体，中文无需字体文件 |
| `artifact.create_web_page` | auto | html | 静态页，走 `/files`，带 CSP sandbox |
| `web.fetch_url` | auto | — | 只读抓取，SSRF 防护见下 |
| `code.run_python` | **per_action** | — | 真实执行，默认逐次确认 |

## 11. 环境差异：fake-ip

`web.fetch_url` 的 SSRF 防护会拒绝私有/保留地址。这在装了 Clash/Surge 这类代理的机器上
会**拒绝所有域名**：代理把域名解析进 `198.18.0.0/15`（IPv4）和 `2001:2::/48`（IPv6），
两个都属于保留段。

处理方式（默认仍然严格）：

```env
# 确认要靠代理出网时再打开
WEB_FETCH_ALLOW_FAKE_IP=true
# 或者精确放行某些网段
WEB_FETCH_ALLOWED_NETS=203.0.113.0/24
```

拒绝时的报错会**说明原因并给出开关名**，而不是只说"指向内网地址"。

环境冒烟测试（`tests/test_environment_smoke.py`）在检测到 fake-ip 时**显式 skip**，
不假装通过——单元测试证明逻辑，这类测试证明"这台机器上真的能用"。

## 12. 运行代码

`code.run_python` 在服务账号下真实执行，因此：

- 默认 `per_action`：模型调用被闸门拦下，用户批准后才执行；
- 环境被剥净（无 API key、无代理变量），`-I` 隔离模式，独立临时目录；
- 20 秒超时后强杀；
- **不宣称是沙箱**：Python 能起子进程，等价于命令行权限，所以默认逐次确认。
