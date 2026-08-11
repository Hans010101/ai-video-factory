"""出图后的画面自检。

只做一件事：抓双人镜头里的「换装」。

FLUX 在多主体提示词上的属性绑定不牢，两个人的衣服会互换 —— 女儿穿走父亲的
深蓝衬衫、父亲套上女儿的米色毛衣。实测同一段提示词换四个 seed，有一个会翻车，
换写法（服装前置 / 方位前置）都救不了，是 seed 决定的。

既然提示词改不掉，就在出图后检出来重打。判据很简单：双人模板一律是
「浅色上衣在左、深色上衣在右」，取两侧胸口区域的亮度一比即可。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

# 胸口取样框，按画面比例给。避开头部（发色干扰）和底部（字幕条压在上面）。
_BAND_TOP, _BAND_BOTTOM = 0.60, 0.88
_LEFT_X = (0.15, 0.40)
_RIGHT_X = (0.60, 0.85)

# 左右亮度差小于这个值就当分不清，不判翻车 —— 宁可漏判也不要误杀，
# 误杀会白白多花一次出图钱。
_MIN_GAP = 18.0


def _patch_luma(img, x0: float, x1: float) -> float:
    w, h = img.size
    box = (int(w * x0), int(h * _BAND_TOP), int(w * x1), int(h * _BAND_BOTTOM))
    px = img.crop(box).convert("RGB").resize((16, 16))
    vals = [0.299 * r + 0.587 * g + 0.114 * b for r, g, b in px.getdata()]
    vals.sort()
    # 取中位数：衣服占多数面积，背景和皮肤是少数，中位数比均值稳。
    return vals[len(vals) // 2]


def wardrobe_swapped(path: Path) -> Optional[bool]:
    """双人镜头里两人的衣服是不是换了。

    返回 True=换了，False=没换，None=判不了（读不出图，或两侧亮度太接近）。
    只对「左浅右深」的双人模板有效，单人镜头不要调。
    """
    try:
        from PIL import Image
        with Image.open(path) as img:
            left = _patch_luma(img, *_LEFT_X)
            right = _patch_luma(img, *_RIGHT_X)
    except Exception:
        return None
    if abs(left - right) < _MIN_GAP:
        return None
    return left < right
