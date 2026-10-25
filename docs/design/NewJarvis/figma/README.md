# Figma 源文件提取

由 `scripts/figma_pull.py` 从 Figma 文件 `UPeWdeWBAu3zupEJ3hmPDS` 的 **Page 2** 生成。
Page 1 为空，全部内容都在 Page 2。

| 文件 | 内容 |
| --- | --- |
| `frames.json` | 14 个顶层帧的结构化摘要：几何、填充、描边、圆角、布局模式、文本与字体 |
| `texts.md` | 逐帧文案表，坐标已换算为帧内原点，按 y→x 排序 |
| `tokens.md` | 实际用到的颜色、字号、字体、圆角及出现次数 |
| `png/` | 14 个帧的 2x 导出（手表 800×1080，中控 2080×宽） |

原始 REST 返回体积 3.3 MB，默认不落盘；需要时加 `--keep-raw`。

## 帧清单

手表 9 帧（400×540）：安静待机、有意义注意力流、四层依据、高风险许可、语音输入、
习惯建议、离线缓存、服务降级、低电量极暗模式。

中控 5 帧（1040×宽）：全量活动流、Agent 编排台、记忆库、权限门控矩阵、设备模型运维。

## 重新拉取

需要 Figma personal access token（`file_content:read`），放在仓库根目录 `.figma_token`
（已在 `.gitignore` 中）或通过 `FIGMA_TOKEN` 环境变量提供。

```powershell
python scripts\figma_pull.py --key UPeWdeWBAu3zupEJ3hmPDS --page "Page 2"
```

只要文案不要图时加 `--no-images`；换倍率用 `--scale 3`。

## 阅读建议

- 想改 UI 文案：看 `texts.md`。
- 想抄视觉规格（色值、字号、圆角、内边距）：看 `tokens.md` 与 `frames.json` 里的 `layout` 字段。
- 只想看图：`png/`，比 `../PNG_Output/` 分辨率更高且与源文件同版本。
