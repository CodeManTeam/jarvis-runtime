"""Pull a Figma file's document tree, tokens and frame images into the repo.

The file only needs a restricted personal access token with `file_content:read`.

    python scripts/figma_pull.py --key UPeWdeWBAu3zupEJ3hmPDS --page "Page 2"

Token lookup order: --token, $FIGMA_TOKEN, <repo>/.figma_token.

Outputs (default under docs/design/NewJarvis/figma):
    document.json   raw REST payload, kept for re-processing
    frames.json     per-frame summary: geometry, texts, fills, radii
    tokens.md       colours, type scale and radii actually used
    png/*.png       2x export of every top-level frame
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

API = "https://api.figma.com/v1"
REPO = Path(__file__).resolve().parent.parent


def token_from(args: argparse.Namespace) -> str:
    if args.token:
        return args.token.strip()
    env = os.getenv("FIGMA_TOKEN")
    if env:
        return env.strip()
    path = Path(args.token_file) if args.token_file else REPO / ".figma_token"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    raise SystemExit("no Figma token: pass --token, set FIGMA_TOKEN, or create .figma_token")


def get(url: str, token: str) -> bytes:
    request = urllib.request.Request(url, headers={"X-Figma-Token": token})
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"{exc.code} {url}: {exc.read()[:200]!r}") from exc


def get_json(url: str, token: str) -> dict:
    return json.loads(get(url, token))


def colour(value: dict | None) -> str | None:
    """Figma stores 0..1 floats; emit #RRGGBB plus alpha when it is not opaque."""
    if not isinstance(value, dict):
        return None
    channels = [value.get(k, 0) for k in ("r", "g", "b")]
    try:
        rgb = [max(0, min(255, round(float(c) * 255))) for c in channels]
        alpha = float(value.get("a", 1))
    except (TypeError, ValueError):
        return None
    hexed = "#%02X%02X%02X" % tuple(rgb)
    return hexed if alpha >= 0.999 else f"{hexed}@{alpha:.2f}"


def solid_fills(node: dict) -> list[str]:
    out = []
    for paint in node.get("fills") or []:
        if paint.get("visible") is False:
            continue
        if paint.get("type") == "SOLID":
            hexed = colour(paint.get("color"))
            if hexed:
                opacity = paint.get("opacity")
                if opacity is not None and opacity < 0.999:
                    hexed = f"{hexed}@{opacity:.2f}"
                out.append(hexed)
    return out


def solid_strokes(node: dict) -> list[str]:
    out = []
    for paint in node.get("strokes") or []:
        if paint.get("visible") is False:
            continue
        if paint.get("type") == "SOLID":
            hexed = colour(paint.get("color"))
            if hexed:
                out.append(hexed)
    return out


def radii(node: dict) -> list[float] | None:
    if node.get("rectangleCornerRadii"):
        return [round(float(v), 2) for v in node["rectangleCornerRadii"]]
    if node.get("cornerRadius") is not None:
        return [round(float(node["cornerRadius"]), 2)] * 4
    return None


def walk(node: dict, out: list[dict], depth: int = 0) -> None:
    box = node.get("absoluteBoundingBox") or {}
    entry = {
        "id": node.get("id"),
        "type": node.get("type"),
        "name": node.get("name"),
        "depth": depth,
        "x": round(box.get("x", 0), 2) if box else None,
        "y": round(box.get("y", 0), 2) if box else None,
        "w": round(box.get("width", 0), 2) if box else None,
        "h": round(box.get("height", 0), 2) if box else None,
    }
    fills = solid_fills(node)
    if fills:
        entry["fills"] = fills
    strokes = solid_strokes(node)
    if strokes:
        entry["strokes"] = strokes
        entry["strokeWeight"] = node.get("strokeWeight")
    box_radius = radii(node)
    if box_radius:
        entry["radius"] = box_radius
    if node.get("layoutMode") in {"HORIZONTAL", "VERTICAL"}:
        entry["layout"] = {
            "mode": node["layoutMode"],
            "gap": node.get("itemSpacing"),
            "padding": [node.get(k) for k in ("paddingTop", "paddingRight", "paddingBottom", "paddingLeft")],
        }
    if node.get("type") == "TEXT":
        style = node.get("style") or {}
        entry["text"] = node.get("characters")
        entry["font"] = {
            "family": style.get("fontFamily"),
            "weight": style.get("fontWeight"),
            "size": style.get("fontSize"),
            "lineHeightPx": round(style.get("lineHeightPx", 0), 2) if style.get("lineHeightPx") else None,
            "letterSpacing": style.get("letterSpacing"),
            "align": style.get("textAlignHorizontal"),
        }
        entry["textColour"] = colour(style.get("fills", [{}])[0].get("color")) if style.get("fills") else None
        if entry["textColour"] is None and fills:
            entry["textColour"] = fills[0]
    out.append(entry)
    for child in node.get("children", []):
        walk(child, out, depth + 1)


def slug(name: str, fallback: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip().strip(".")
    return (cleaned or fallback)[:80]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--key", required=True, help="Figma file key")
    parser.add_argument("--page", default=None, help="page name; defaults to the first non-empty page")
    parser.add_argument("--out", default=str(REPO / "docs/design/NewJarvis/figma"))
    parser.add_argument("--token")
    parser.add_argument("--token-file")
    parser.add_argument("--scale", type=float, default=2.0)
    parser.add_argument("--no-images", action="store_true")
    parser.add_argument("--keep-raw", action="store_true", help="also write the raw REST payload")
    args = parser.parse_args()

    token = token_from(args)
    out = Path(args.out)
    (out / "png").mkdir(parents=True, exist_ok=True)

    print("fetching document ...", flush=True)
    document = get_json(f"{API}/files/{args.key}", token)
    if args.keep_raw:
        (out / "document.json").write_text(
            json.dumps(document, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    pages = document["document"]["children"]
    page = next((p for p in pages if args.page and p["name"] == args.page), None)
    if page is None:
        page = next((p for p in pages if p.get("children")), pages[0])
    frames = [c for c in page.get("children", []) if c.get("type") == "FRAME"]
    print(f"page {page['name']!r}: {len(frames)} top-level frames", flush=True)

    colours: Counter = Counter()
    sizes: Counter = Counter()
    families: Counter = Counter()
    radiuses: Counter = Counter()
    summary = []
    for frame in frames:
        nodes: list[dict] = []
        walk(frame, nodes)
        box = frame.get("absoluteBoundingBox") or {}
        summary.append({
            "id": frame["id"],
            "name": frame["name"],
            "size": [round(box.get("width", 0), 2), round(box.get("height", 0), 2)],
            "fill": solid_fills(frame),
            "radius": radii(frame),
            "node_count": len(nodes),
            "nodes": nodes,
        })
        for node in nodes:
            for value in (node.get("fills") or []) + (node.get("strokes") or []):
                colours[value] += 1
            if node.get("textColour"):
                colours[node["textColour"]] += 1
            font = node.get("font") or {}
            if font.get("size"):
                sizes[font["size"]] += 1
            if font.get("family"):
                families[f"{font['family']} {font.get('weight')}"] += 1
            if node.get("radius"):
                radiuses[tuple(node["radius"])] += 1

    (out / "frames.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    # Flat copy deck: absolute canvas coordinates rebased onto each frame, so the
    # numbers can be read straight off as in-frame layout positions.
    deck = ["# 逐帧文案与几何", "", "坐标已换算为帧内左上角原点，按 y 再 x 排序。", ""]
    for frame, item in zip(frames, summary):
        origin = frame.get("absoluteBoundingBox") or {}
        ox, oy = origin.get("x", 0), origin.get("y", 0)
        deck.append(f"## {item['name']}")
        deck.append("")
        deck.append(f"- 帧 `{item['id']}`，{item['size'][0]:.0f}×{item['size'][1]:.0f}，节点 {item['node_count']}")
        if item["fill"]:
            deck.append(f"- 底色 {', '.join(item['fill'])}")
        if item["radius"]:
            deck.append(f"- 圆角 {item['radius']}")
        deck.append("")
        deck.append("| y | x | 字号 | 字重 | 文本 |")
        deck.append("| --- | --- | --- | --- | --- |")
        texts = [n for n in item["nodes"] if n.get("text")]
        texts.sort(key=lambda n: (round((n["y"] or 0) - oy), round((n["x"] or 0) - ox)))
        for node in texts:
            font = node.get("font") or {}
            text = (node["text"] or "").replace("|", "\\|").replace("\n", " ⏎ ")
            if len(text) > 140:
                text = text[:137] + "…"
            deck.append(
                f"| {(node['y'] or 0) - oy:.0f} | {(node['x'] or 0) - ox:.0f} | "
                f"{font.get('size')} | {font.get('weight')} | {text} |"
            )
        deck.append("")
    (out / "texts.md").write_text("\n".join(deck), encoding="utf-8")

    lines = [f"# Figma 提取：{document.get('name')} / {page['name']}", ""]
    lines.append(f"- 文件版本 `{document.get('version')}`，最后修改 {document.get('lastModified')}")
    lines.append(f"- 顶层帧 {len(frames)} 个，节点总数 {sum(f['node_count'] for f in summary)}")
    lines += ["", "## 帧清单", "", "| 帧 | ID | 尺寸 | 节点数 |", "| --- | --- | --- | --- |"]
    for item in summary:
        lines.append(f"| {item['name']} | `{item['id']}` | {item['size'][0]:.0f}×{item['size'][1]:.0f} | {item['node_count']} |")
    lines += ["", "## 颜色（出现次数）", "", "| 色值 | 次数 |", "| --- | --- |"]
    for value, count in colours.most_common():
        lines.append(f"| `{value}` | {count} |")
    lines += ["", "## 字号（出现次数）", "", "| 字号 | 次数 |", "| --- | --- |"]
    for value, count in sorted(sizes.items()):
        lines.append(f"| {value} | {count} |")
    lines += ["", "## 字体", "", "| 字体 | 次数 |", "| --- | --- |"]
    for value, count in families.most_common():
        lines.append(f"| {value} | {count} |")
    lines += ["", "## 圆角", "", "| 圆角 | 次数 |", "| --- | --- |"]
    for value, count in radiuses.most_common():
        lines.append(f"| {list(value)} | {count} |")
    (out / "tokens.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {out/'frames.json'} and tokens.md", flush=True)

    if args.no_images or not frames:
        return 0

    ids = ",".join(frame["id"] for frame in frames)
    images = get_json(f"{API}/images/{args.key}?ids={ids}&format=png&scale={args.scale}", token)
    if images.get("err"):
        print("image export failed:", images["err"], file=sys.stderr)
        return 1
    mapping = images.get("images") or {}
    print(f"exporting {len(mapping)} frames at {args.scale}x ...", flush=True)
    for frame in frames:
        url = mapping.get(frame["id"])
        if not url:
            print(f"  missing image for {frame['name']}", file=sys.stderr)
            continue
        blob = get(url, token)
        target = out / "png" / f"{slug(frame['name'], frame['id'].replace(':', '-'))}.png"
        target.write_bytes(blob)
        print(f"  {target.name}  {target.stat().st_size/1024:.0f} KB", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
