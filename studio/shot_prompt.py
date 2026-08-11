"""镜头提示词构建器。

方法来自 Emily2040/seedance-2.0 的 seedance-prompt / antislop / camera /
lighting 四个技能模块，核心是三条：

1. 拍摄简报公式 —— Subject + Action + Scene + Camera + Lighting + Constraints。
   主体和动作必须放最前，靠前的从句决定画面层级。

2. 可见性检验 —— 每个短语都要「相机拍得到、测光表量得到、能听见、或看得见
   在动」。过不了这关的词就是废话，要换成制作语言。
   cinematic → locked close-up, warm practical key
   beautiful → pearl highlights on wet ceramic
   professional → clean tabletop, controlled reflection

3. 否定会召唤 —— 「no text」写在正向提示词里反而会把文字召唤出来。
   之前画面里反复出现乱码汉字，根源就在这。否定只能放在 negative_prompt。
"""

from __future__ import annotations

import re
from typing import Any

# 可见性检验没通过的词。留着它们等于让模型自由发挥，
# 而模型对「电影感」的理解和你想要的多半不是一回事。
SLOP_WORDS = (
    "cinematic", "epic", "stunning", "beautiful", "gorgeous", "dynamic",
    "professional", "masterpiece", "high quality", "best quality", "8k", "4k",
    "ultra detailed", "highly detailed", "trending on artstation", "award winning",
    "breathtaking", "vibrant", "atmospheric", "aesthetic", "artistic",
)


def strip_slop(text: str) -> tuple[str, list[str]]:
    """删掉空洞词，返回（清理后的文本, 被删的词）。"""
    removed: list[str] = []
    out = text
    for w in SLOP_WORDS:
        pattern = re.compile(rf"\b{re.escape(w)}\b[,\s]*", re.IGNORECASE)
        if pattern.search(out):
            removed.append(w)
            out = pattern.sub("", out)
    out = re.sub(r"\s{2,}", " ", out)
    out = re.sub(r"(,\s*){2,}", ", ", out).strip(" ,")
    return out, removed


# 情绪 → 具体的镜头与光。不写「悲伤的画面」，写「什么机位、什么光源」。
# 表格取自 seedance-camera / seedance-lighting 的对照，按情感类内容裁剪。
MOOD_GRAMMAR: dict[str, dict[str, str]] = {
    "tension": {
        "camera": "locked medium shot, subjects held at opposite thirds of the frame",
        "light": "single warm practical lamp from frame left, long soft shadows across the wall",
    },
    "weary": {
        "camera": "static medium close-up, subject seated and slightly off-centre",
        "light": "flat overcast window light from the right, no rim separation",
    },
    "distance": {
        "camera": "wide two-shot, empty space held between the subjects",
        "light": "cool late-afternoon window light, one side of the room falling into shadow",
    },
    "reflection": {
        "camera": "slow push-in from medium to close, ending on the hands",
        "light": "warm low lamp, background dropping into soft darkness",
    },
    "resolve": {
        "camera": "eye-level close-up, subject facing frame front",
        "light": "clean soft key from the front, gentle fill, background evenly lit",
    },
    "neutral": {
        "camera": "eye-level medium shot, subject centred with headroom",
        "light": "soft window light from frame left, gentle bounce on the opposite side",
    },
}

# 中文情绪线索 → 镜头语法键。观点/情感类文案的常见词。
_MOOD_CUES: list[tuple[tuple[str, ...], str]] = [
    (("冲突", "争吵", "指责", "发火", "对峙", "紧张"), "tension"),
    (("累", "疲惫", "委屈", "消耗", "忍", "扛", "沉默"), "weary"),
    (("疏远", "距离", "隔阂", "冷淡", "各自", "边界"), "distance"),
    (("理解", "回想", "反思", "明白", "意识到", "其实"), "reflection"),
    (("应该", "可以说", "改变", "决定", "开始", "不是谁"), "resolve"),
]


def detect_mood(text: str) -> str:
    """从旁白里读出情绪，决定这一镜的镜头与光。

    此前每一镜都用同一套风格提示词，讲隐忍疲惫时配出一家人其乐融融的画面
    —— 因为提示词里根本没有情绪信息。
    """
    for cues, mood in _MOOD_CUES:
        if any(c in text for c in cues):
            return mood
    return "neutral"


# 画面风格：只描述介质与画法，不含情绪和构图 —— 那两项由 MOOD_GRAMMAR 提供。
STYLE_MEDIUM: dict[str, str] = {
    "comic": ("modern Chinese editorial illustration, clean ink linework, "
              "flat muted colour, one warm accent"),
    "cinematic": ("photographic still, 35mm natural perspective, "
                  "shallow depth of field, muted film grade"),
    "ink": ("Chinese ink-wash illustration, restrained brushwork, "
            "wide negative space, single warm accent"),
}

# 否定只出现在这里。写进正向提示词会把这些东西召唤出来。
NEGATIVE = ("text, letters, chinese characters, words, captions, signage, "
            "watermark, logo, signature, "
            "cut off head, cropped face, deformed hands, extra limbs, "
            "crowd, background people")


# 角色一致性。方法取自 seedance-characters：身份锚点 = 年龄段 + 轮廓 +
# 发型 + 服装，逐镜原样复述；走位和表情每镜单独给。
#
# 这两个模型都不支持参考图，只有 seed。所以一致性完全靠「把同一段外貌
# 描述钉进每一镜」+ 固定 seed 来维持 —— 不能指望模型自己记住上一镜。
CAST_TEMPLATES: dict[str, str] = {
    "adult_child": (
        "Character A: a woman in her early thirties, slim build, "
        "shoulder-length dark hair tied back, plain oatmeal knit sweater. "
        "Character B: a man in his sixties, stocky, short grey hair, "
        "dark blue button shirt"
    ),
    "couple": (
        "Character A: a woman in her early thirties, slim build, "
        "shoulder-length dark hair tied back, plain oatmeal knit sweater. "
        "Character B: a man in his mid thirties, medium build, "
        "short black hair, charcoal grey crewneck"
    ),
    "single": (
        "Character A: a woman in her early thirties, slim build, "
        "shoulder-length dark hair tied back, plain oatmeal knit sweater"
    ),
}

# 中文线索 → 该用哪组人物。选错人物组比长相不一致更出戏。
_CAST_CUES: list[tuple[tuple[str, ...], str]] = [
    (("父母", "爸妈", "养育", "供你", "长辈", "母亲", "父亲", "儿女"), "adult_child"),
    (("婚姻", "伴侣", "夫妻", "另一半", "两个人"), "couple"),
]


def detect_cast(full_text: str) -> str:
    for cues, cast in _CAST_CUES:
        if any(c in full_text for c in cues):
            return cast
    return "single"


def cast_seed(full_text: str) -> int:
    """由脚本内容派生一个稳定 seed —— 同一篇脚本每次出片长相一致，
    不同脚本之间又各不相同。"""
    import hashlib
    return int(hashlib.md5(full_text[:200].encode()).hexdigest()[:7], 16) % 2_000_000


def build(scene_text: str, style: str = "comic",
          subject_hint: str = "", cast: str = "", ) -> dict[str, Any]:
    """按拍摄简报公式生成一镜的提示词。

    scene_text 是该镜的中文旁白（用来判定情绪），subject_hint 是已翻成英文
    的画面主体。返回 {prompt, negative_prompt, mood}。
    """
    mood = detect_mood(scene_text)
    grammar = MOOD_GRAMMAR.get(mood, MOOD_GRAMMAR["neutral"])
    medium = STYLE_MEDIUM.get(style, STYLE_MEDIUM["comic"])

    subject = (subject_hint or "two people in a quiet apartment").strip()
    subject, _ = strip_slop(subject)

    # 身份锚点必须逐镜原样复述 —— 模型不记得上一镜是谁，
    # 少写一次这一镜的人就换脸了。
    identity = CAST_TEMPLATES.get(cast, "") if cast else ""

    # 顺序即层级：人物身份 → 主体动作 → 镜头 → 光 → 介质。
    parts = []
    if identity:
        parts.append(identity + ".")
    parts += [
        f"{subject}.",
        f"{grammar['camera']}.",
        f"{grammar['light']}.",
        f"{medium}.",
        "16:9 frame, subjects fully inside the frame with headroom.",
    ]
    return {"prompt": " ".join(parts), "negative_prompt": NEGATIVE,
            "mood": mood, "cast": cast}
