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


# 中文版可见性检验。这些词相机拍不到，模型没法「画」出来，就退而求其次
# 把它们**写**出来 —— 画面里出现「底层机制」四个大字的海报标题，根源在此。
# 中文出图模型排版汉字是强项，所以这个倾向比英文模型明显得多。
CN_SLOP_WORDS = (
    "底层机制", "机制", "逻辑", "本质", "内核", "内在", "抽象",
    "寓言式", "寓言", "象征意义", "象征", "隐喻", "意象", "概念",
    "主体性", "边界感", "张力", "情绪张力", "现实瞬间", "画面感",
    "叙事", "表达", "呈现", "体现", "传达", "映射",
)
_CN_SLOP_RE = re.compile("|".join(re.escape(w) for w in CN_SLOP_WORDS))


def strip_cn_slop(text: str) -> str:
    out = _CN_SLOP_RE.sub("", text)
    out = re.sub(r"[、，,]{2,}", "，", out)
    out = re.sub(r"[的地得]{2,}", "的", out)
    return out.strip(" ,.、，。的")


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


# 用户脚本的「统一风格」里常写「画面无文字、无水印」。这类否定翻成英文后
# 混进正向提示词，反而会把文字召唤出来 —— 之前画面里反复出现乱码汉字，
# 一半来源在这。否定一律剥掉，同样的内容 NEGATIVE 里已经有了。
_NEG_NOUN = (r"(?:visible\s+)?(?:text|texts|words|letters|characters|writing|"
             r"captions?|subtitles?|watermarks?|logos?|signatures?|labels?|signage)")
# 要把整串一起吃掉。「no text or watermark」只删掉「no text」的话，
# 会剩下半截「or watermark」，等于否定还在提示词里。
_NEGATION_RE = re.compile(
    rf"\b(?:no|without|free\s+of|avoid(?:ing)?|excluding)\s+(?:any\s+)?{_NEG_NOUN}"
    rf"(?:(?:\s*,\s*(?:or\s+|and\s+)?|\s+(?:or|and)\s+|\s*/\s*)"
    rf"(?:any\s+)?{_NEG_NOUN})*"
    rf"(?:\s+(?:in|on)\s+the\s+(?:image|frame|picture|shot))?[\s,;.]*",
    re.IGNORECASE)


# 中文也要剥。翻译走的是 Gemini，配额耗尽时会原样返回中文，
# 「画面无文字、无水印」就这样原封不动进了模型。
_CN_NEG_NOUN = r"(?:文字|文本|字幕|水印|logo|标志|标识|签名|字样)"
_CN_NEGATION_RE = re.compile(
    rf"(?:画面|图中|图片|背景)?\s*"
    rf"(?:无|没有|不要|不得|不能|不可|不含|不带|不加|禁止|避免|去掉|去除)\s*"
    rf"(?:出现|包含|含有|含|带有|带|有)?\s*(?:任何|一切)?\s*{_CN_NEG_NOUN}"
    rf"(?:\s*[、，,和或]\s*(?:无|没有|不要)?\s*{_CN_NEG_NOUN})*\s*[。，,、;；]?")


# 引号里的内容会被当成「要画上去的字」。脚本的画面建议常写成
# 「主角正面遭遇"感恩，不等于交出人生"的现实瞬间」，那句引文就原样出现在
# 画面里，变成一张海报标题。中文出图模型（wan / qwen）尤其吃这一套，
# 因为它们本来就擅长排版汉字。
_QUOTED_RE = re.compile(r"[\"“”‘’'「」『』《》〈〉]+[^\"“”‘’'「」『』《》〈〉]*"
                        r"[\"“”‘’'「」『』《》〈〉]+")


def strip_quoted(text: str) -> str:
    out = _QUOTED_RE.sub("", text)
    out = re.sub(r"\s{2,}", " ", out)
    out = re.sub(r"[、，,]{2,}", "，", out)
    return out.strip(" ,.、，。")


def strip_negation(text: str) -> str:
    out = _CN_NEGATION_RE.sub("", text)
    out = _NEGATION_RE.sub("", out)
    out = re.sub(r"\s{2,}", " ", out)
    out = re.sub(r"(,\s*){2,}", ", ", out)
    out = re.sub(r"[、，,]{2,}", "，", out)
    return out.strip(" ,.、，。")


# 情绪 → 具体的镜头与光。不写「悲伤的画面」，写「什么机位、什么光源」。
# 表格取自 seedance-camera / seedance-lighting 的对照，按情感类内容裁剪。
MOOD_GRAMMAR: dict[str, dict[str, str]] = {
    # camera 是双人构图，camera1 是同一情绪下的单人替代 —— 单人镜头套上
    # 「双人镜」会逼模型硬塞第二个人进来。没有 camera1 的情绪，两种都能用。
    "tension": {
        "camera": "locked medium shot, subjects held at opposite thirds of the frame",
        "camera1": "locked medium shot, subject pressed to one third of the frame, "
                   "empty wall filling the rest",
        "light": "single warm practical lamp from frame left, long soft shadows across the wall",
        "face": "jaw set, lips pressed thin, eyes fixed and unblinking, not smiling",
    },
    "weary": {
        "camera": "static medium close-up, subject seated and slightly off-centre",
        "light": "flat overcast window light from the right, no rim separation",
        "face": "eyelids heavy, shoulders dropped, mouth slack, gaze aimed at nothing",
    },
    "distance": {
        "camera": "wide two-shot, empty space held between the subjects",
        "camera1": "wide shot, subject small against a large empty room",
        "light": "cool late-afternoon window light, one side of the room falling into shadow",
        "face": "faces turned away from each other, neither meeting the other's eyes",
    },
    "reflection": {
        "camera": "slow push-in from medium to close, ending on the hands",
        "light": "warm low lamp, background dropping into soft darkness",
        "face": "eyes lowered, brow softened, lips parted as if about to speak",
    },
    "resolve": {
        "camera": "eye-level close-up, subject facing frame front",
        "light": "clean soft key from the front, gentle fill, background evenly lit",
        "face": "chin level, steady direct gaze, mouth calm and closed",
    },
    "neutral": {
        "camera": "eye-level medium shot, subject centred with headroom",
        "light": "soft window light from frame left, gentle bounce on the opposite side",
        "face": "relaxed face, eyes open and attentive, no exaggerated expression",
    },
}

# 中文情绪线索 → 镜头语法键。观点/情感类文案的常见词。
_MOOD_CUES: list[tuple[tuple[str, ...], str]] = [
    # 责备多以引语出现（「我们为你付出这么多，你怎么能不听」），
    # 只匹配「冲突/争吵」这类概括词会全部漏掉，落成中性表情配指责台词。
    (("冲突", "争吵", "指责", "发火", "对峙", "紧张",
      "怎么能", "白养", "不听话", "翻脸", "逼你", "凭什么", "不孝"), "tension"),
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
# 中英各写一份。翻译（走 Gemini）配额耗尽时提示词会退回中文，
# 这时纯英文负向压不住中文出图模型 —— 它们本来就擅长排版汉字，
# 会把提示词里的句子当成要画的标题、把画面建议当成信息图的标注。
NEGATIVE = ("text, letters, chinese characters, words, captions, signage, "
            "watermark, logo, signature, poster title, infographic, "
            "diagram labels, callout lines, "
            "cut off head, cropped face, deformed hands, extra limbs, "
            "crowd, background people, "
            "文字，汉字，字幕，标题，海报文字，标注，说明文字，引线，信息图，"
            "水印，签名，人物出画，多余的手")


# 角色一致性。方法取自 seedance-characters：身份锚点 = 年龄段 + 轮廓 +
# 发型 + 服装，逐镜原样复述；走位和表情每镜单独给。
#
# 这两个模型都不支持参考图，所以一致性完全靠「把同一段外貌描述钉进每一镜」
# 来维持，不能指望模型记住上一镜。seed 只管可复现，不管一致性。
#
# 写法有三条硬约束，都是踩出来的：
# 1. 人种要写死。不写就按训练分布随机取脸，父亲上一镜东亚、下一镜西方。
# 2. 服装紧跟在人物名词后面（… man in his sixties, wearing a dark blue shirt），
#    分开写模型会把两个人的衣服对调。
# 3. 不要用 Character A / B 这种标签。那是给支持参考图的模型看的记号，
#    FLUX 读不懂，反而会把两套特征叠成一个缝合怪。改用左右方位的自然语序。
_WOMAN = ("a Han Chinese woman in her early thirties, oval face, slim build, "
          "black hair in a low ponytail, wearing a plain oatmeal knit sweater")
_FATHER = ("a Han Chinese man in his sixties, broad square face, stocky, "
           "short grey hair, wearing a dark blue button shirt")
_HUSBAND = ("a Han Chinese man in his mid thirties, medium build, "
            "short black hair, wearing a charcoal grey crewneck")

CAST_TEMPLATES: dict[str, tuple[str, str]] = {
    "adult_child": (_WOMAN, _FATHER),
    "couple": (_WOMAN, _HUSBAND),
    "single": (_WOMAN, ""),
}

# 这一镜在讲「他们」还是在讲「你」。决定画面里放几个人。
_TWO_PERSON_CUES = ("父母", "爸妈", "他们", "长辈", "母亲", "父亲",
                    "对方", "两个人", "伴侣", "家人", "彼此")


def shot_cast(scene_text: str, cast: str) -> tuple[str, int]:
    """返回（该镜的身份锚点, 画面人数）。

    单人镜头只给一个角色的描述，并明确写「画面里只有一个人」——
    否则模型会自作主张把另一个角色也画进来，或者跟主角缝在一起。
    """
    a, b = CAST_TEMPLATES.get(cast, CAST_TEMPLATES["single"])
    if b and any(c in scene_text for c in _TWO_PERSON_CUES):
        return (f"On the left of the frame stands {a}. "
                f"On the right of the frame stands {b}. "
                "Two separate people, a clear gap between them, "
                "each wearing only their own clothes"), 2
    return f"One person alone in the frame: {a}", 1

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
    """由脚本内容派生一个基准 seed，让同一篇脚本每次出片结果可复现。

    这是**基准值**，调用方要按镜号错开（seed + i）。九镜共用一个 seed 会把
    同一个坏构图复制九遍，而不是让人物更像 —— 一致性归身份锚点管。
    """
    import hashlib
    return int(hashlib.md5(full_text[:200].encode()).hexdigest()[:7], 16) % 2_000_000


def build(scene_text: str, style: str = "comic", subject_hint: str = "",
          cast: str = "", medium_override: str = "") -> dict[str, Any]:
    """按拍摄简报公式生成一镜的提示词。

    scene_text 是该镜的中文旁白（用来判定情绪），subject_hint 是已翻成英文
    的画面主体，medium_override 是脚本自带的「统一风格」（已翻成英文）。

    返回 {prompt, negative_prompt, mood, cast, headcount}。
    """
    mood = detect_mood(scene_text)
    grammar = MOOD_GRAMMAR.get(mood, MOOD_GRAMMAR["neutral"])
    # 脚本写了统一风格就以它为准，不要和平台模板并列 —— 两套介质描述
    # 同时出现（「写实人物」+「ink linework」），模型每镜各挑一条。
    medium = strip_negation(medium_override.strip()) or \
        STYLE_MEDIUM.get(style, STYLE_MEDIUM["comic"])

    subject = (subject_hint or "two people in a quiet apartment").strip()
    subject, _ = strip_slop(subject)
    subject = strip_cn_slop(strip_negation(strip_quoted(subject)))
    # 全被剥光说明这一镜的画面建议整句都是拍不到的概念。给个中性的具体
    # 场景兜底，比把概念词丢给模型强 —— 后者只会把词写在画面上。
    if len(subject) < 4:
        subject = "两个人在安静的房间里相对而立"

    # 身份锚点必须逐镜原样复述 —— 模型不记得上一镜是谁，
    # 少写一次这一镜的人就换脸了。
    identity, headcount = shot_cast(scene_text, cast) if cast else ("", 0)

    # 顺序即层级：人物身份 → 主体动作 → 镜头 → 光 → 介质。
    parts = []
    if identity:
        parts.append(identity + ".")
    camera = (grammar.get("camera1") or grammar["camera"]) if headcount == 1 \
        else grammar["camera"]
    parts += [
        f"{subject}.",
        # 表情要单独写。只给机位和光，讲对峙的台词也会配出满脸堆笑的画面 ——
        # 模型对「人物照片」的默认先验就是微笑，不写死就压不住。
        f"{grammar['face']}.",
        f"{camera}.",
        f"{grammar['light']}.",
        f"{medium}.",
        "16:9 frame, subjects fully inside the frame with headroom.",
    ]
    return {"prompt": " ".join(parts), "negative_prompt": NEGATIVE,
            "mood": mood, "cast": cast, "headcount": headcount}
