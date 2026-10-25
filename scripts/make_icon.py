"""生成面板图标（assets/workflow.ico）。

图案：深蓝圆角方块 + 白色循环箭头 —— 对应「循环体直到审核通过」这一核心结构。
只在需要重新生成图标时运行：
    python scripts/make_icon.py
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "workflow.ico"

SIZE = 256
BLUE = (31, 111, 235, 255)
BLUE_DARK = (16, 56, 122, 255)
WHITE = (255, 255, 255, 255)


def render(size: int) -> Image.Image:
    scale = size / SIZE
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 背景：深蓝圆角方块
    pad = int(10 * scale)
    radius = int(56 * scale)
    d.rounded_rectangle([pad, pad, size - pad, size - pad], radius=radius, fill=BLUE_DARK)

    # 循环箭头：开口圆环 + 相切箭头
    cx = cy = size / 2
    r = size * 0.27
    width = max(2, int(20 * scale))
    d.arc([cx - r, cy - r, cx + r, cy + r], start=-40, end=245, fill=WHITE, width=width)

    theta = math.radians(245)
    px, py = cx + r * math.cos(theta), cy + r * math.sin(theta)
    tx, ty = -math.sin(theta), math.cos(theta)   # 顺时针切线
    nx, ny = math.cos(theta), math.sin(theta)    # 法线

    head_len = size * 0.13
    head_half = size * 0.085
    tip = (px + tx * head_len * 0.75, py + ty * head_len * 0.75)
    base = (tip[0] - tx * head_len, tip[1] - ty * head_len)
    p2 = (base[0] + nx * head_half, base[1] + ny * head_half)
    p3 = (base[0] - nx * head_half, base[1] - ny * head_half)
    d.polygon([tip, p2, p3], fill=WHITE)

    return img


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    base = render(SIZE)
    base.save(OUT, format="ICO",
              sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"已生成 {OUT}")


if __name__ == "__main__":
    main()
