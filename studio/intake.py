"""智能派单：把脚本 / 文档解析成分镜，再自动匹配工具并编排成工序链。

流程：
    文档或文本 → 解析分镜表 → 推断所需能力 → 用 lib.scoring 的 7 维评分
    在「当前可用」的工具里选出最佳 → 生成工序链 → 下发到任务队列

选型用的是项目自带的评分引擎（task_fit / output_quality / control /
reliability / cost_efficiency / latency / continuity），不是另造一套规则，
这样和流水线里智能体的选型口径一致。
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

from lib.scoring import normalize_task_context, rank_providers
from tools.tool_registry import registry

from studio import i18n

# ---- 分镜识别标记 ----
_NARRATION_TAGS = ("旁白", "配音", "解说", "口播", "台词", "narration", "voiceover", "vo")
_VISUAL_TAGS = ("画面", "视觉", "镜头", "视频", "素材", "visual", "shot", "b-roll", "broll")

# 实际写脚本时未必带冒号，常见写法是「画面提示词 现代都市插画…」这样用空格
# 分隔。这些标记只在行首整词匹配，所以不会误伤正文里出现的同名词。
_BARE_MARKERS = (
    "画面提示词", "视觉提示词", "画面描述", "画面内容", "画面提示", "视觉提示",
    "提示词", "分镜画面", "image prompt", "visual prompt", "prompt",
    "旁白文案", "配音文案", "解说词",
)
_BARE_TAG = re.compile(
    r"^\s*[\[【(]?\s*(" + "|".join(re.escape(m) for m in
                                  sorted(_BARE_MARKERS, key=len, reverse=True))
    + r")\s*[\]】)]?[\s　]+(.+)$",
    re.IGNORECASE,
)

# 文档里更常见的是标记独占一行、内容写在后面几行（Word 里尤其如此）。
# 整行只有标记时不会有歧义，所以这里可以放宽到「文案」「正文」这类词。
_SECTION_VISUAL = (
    "画面提示词", "视觉提示词", "画面描述", "画面内容", "画面提示", "视觉提示",
    "提示词", "画面", "视觉", "分镜画面", "image prompt", "visual prompt", "prompt",
)
_SECTION_NARRATION = (
    "旁白", "配音", "解说", "解说词", "口播", "台词", "文案", "封面文案",
    "正文", "内容", "narration", "voiceover", "script",
)
_SECTION_LINE = re.compile(r"^\s*[\[【(]?\s*([^\s\[\]【】()：:]+)\s*[\]】)]?\s*[:：]?\s*$")


def _section_role(line: str) -> str:
    """整行只有一个标记时，返回它引导的角色：narration / visual / 空。"""
    m = _SECTION_LINE.match(line)
    if not m:
        return ""
    word = m.group(1).strip().lower()
    if word in {w.lower() for w in _SECTION_VISUAL}:
        return "visual"
    if word in {w.lower() for w in _SECTION_NARRATION}:
        return "narration"
    return ""
# 两种常见的分镜标题写法都要认：
#   「场景1」「镜头 2」「scene 3」   —— 关键词在前
#   「第1镜」「第2幕」「第三段」      —— 序号在中间
_SCENE_HEAD = re.compile(
    r"^(?:#{1,6}\s*)?(?:"
    r"(?:场景|镜头|片段|段落|scene|shot|part)\s*[:：#]?\s*(\d+)?"
    # 后面必须是行尾或分隔符 —— 否则「第一段话。」这种正常行文会被
    # 误判成分镜标题，把整段吞掉。
    r"|第\s*(\d+|[一二三四五六七八九十百]+)\s*(?:镜|幕|段|场|节|部分)(?=$|[\s　:：.、·\-—|])"
    r")[.、:：]?\s*(.*)$",
    re.IGNORECASE,
)
_LIST_HEAD = re.compile(r"^\s*(?:\d+[.、)]|[-*+])\s+(.*)$")
_TAG_LINE = re.compile(r"^\s*[\[【(]?\s*([一-龥A-Za-z\-]+)\s*[\]】)]?\s*[:：]\s*(.+)$")


@dataclass
class Scene:
    index: int
    title: str = ""
    narration: str = ""
    visual: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Brief:
    title: str = ""
    scenes: list[Scene] = field(default_factory=list)
    style: str = ""
    raw_chars: int = 0
    raw_text: str = ""     # 原始脚本 —— 一键成片要把它整段交给合成工具

    @property
    def narration_text(self) -> str:
        return "\n".join(s.narration for s in self.scenes if s.narration)

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "style": self.style,
            "raw_chars": self.raw_chars,
            "scenes": [s.as_dict() for s in self.scenes],
            "scene_count": len(self.scenes),
            "narration_chars": len(self.narration_text),
            "visual_count": sum(1 for s in self.scenes if s.visual),
        }


# ---------------- 文档解析 ----------------

def extract_text(filename: str, data: bytes) -> str:
    """从上传的文件里取出纯文本。支持 txt / md / docx / pdf。"""
    name = (filename or "").lower()
    if name.endswith(".docx"):
        try:
            import docx
            doc = docx.Document(io.BytesIO(data))
            return "\n".join(p.text for p in doc.paragraphs)
        except Exception as exc:
            raise ValueError(f"docx 解析失败：{exc}") from exc
    if name.endswith(".pdf"):
        try:
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(data))
            return "\n".join((page.extract_text() or "") for page in reader.pages)
        except Exception as exc:
            raise ValueError(f"pdf 解析失败：{exc}") from exc
    for enc in ("utf-8", "gb18030", "utf-16", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("无法识别文件编码，请另存为 UTF-8 文本")


def _strip_tag(line: str) -> tuple[str, str]:
    """把「旁白：xxx」或「画面提示词 xxx」拆成 (tag, content)。

    带冒号的写法用通用规则；不带冒号的只认白名单里的标记，避免把正文
    第一个词误判成角色标记。
    """
    m = _TAG_LINE.match(line)
    if m:
        return m.group(1).strip().lower(), m.group(2).strip()
    m = _BARE_TAG.match(line)
    if m:
        return m.group(1).strip().lower(), m.group(2).strip()
    return "", line.strip()


def parse_brief(text: str) -> Brief:
    """把自由格式的脚本解析成分镜表。

    识别三种写法，混用也能处理：
      1. 「场景 1」「镜头2:」「## 场景」等标题分段
      2. 段落内用「旁白：」「画面：」标注角色
      3. 完全无标记 —— 按空行分段，每段视为一条旁白
    """
    lines = [ln.rstrip() for ln in (text or "").replace("\r\n", "\n").split("\n")]
    brief = Brief(raw_chars=len(text or ""), raw_text=text or "")

    # 标题：第一条非空行，若像 markdown 标题或很短则采用。
    # 以句末标点结尾的不算 —— 那是正文第一句，当成标题会把它整句吞掉。
    title_line = -1
    for idx, ln in enumerate(lines):
        if ln.strip():
            candidate = re.sub(r"^#+\s*", "", ln).strip()
            looks_like_sentence = candidate.endswith(("。", "！", "？", ".", "!", "?", "；", ";"))
            if (len(candidate) <= 60 and not looks_like_sentence
                    and not _SCENE_HEAD.match(ln) and not _strip_tag(ln)[0]):
                brief.title = candidate
                title_line = idx
            break

    scenes: list[Scene] = []
    current: Optional[Scene] = None
    pending_blank = False
    section_role = ""   # 由「画面提示词」这类独占一行的标记设置

    def ensure_scene() -> Scene:
        nonlocal current
        if current is None:
            current = Scene(index=len(scenes) + 1)
            scenes.append(current)
        return current

    for idx, ln in enumerate(lines):
        # 已经取作标题的那一行不再当正文，否则标题会重复出现在第一镜旁白里
        if idx == title_line:
            continue
        stripped = ln.strip()
        if not stripped:
            pending_blank = True
            continue

        head = _SCENE_HEAD.match(stripped)
        # group(1)=「场景1」的序号，group(2)=「第1镜」的序号，group(3)=标题余文
        if head and (head.group(1) or head.group(2) or head.group(3)):
            current = Scene(index=len(scenes) + 1, title=(head.group(3) or "").strip())
            scenes.append(current)
            pending_blank = False
            continue

        # 整行只有一个标记（Word 文档里最常见的写法）：它引导后面若干行，
        # 直到下一个标记或分镜标题为止。
        role = _section_role(stripped)
        if role:
            section_role = role
            pending_blank = False
            continue

        # 去掉列表符号
        lm = _LIST_HEAD.match(stripped)
        if lm:
            stripped = lm.group(1).strip()

        tag, content = _strip_tag(stripped)
        if not content:
            continue

        if tag and any(t in tag for t in _NARRATION_TAGS):
            section_role = ""
            sc = ensure_scene()
            sc.narration = (sc.narration + " " + content).strip() if sc.narration else content
            pending_blank = False
            continue
        if tag and any(t in tag for t in _VISUAL_TAGS):
            section_role = ""
            sc = ensure_scene()
            sc.visual = (sc.visual + " " + content).strip() if sc.visual else content
            pending_blank = False
            continue

        # 处于某个标记段落之内：整行都归该角色
        if section_role == "visual":
            sc = ensure_scene()
            sc.visual = (sc.visual + " " + content).strip() if sc.visual else content
            pending_blank = False
            continue
        if section_role == "narration":
            sc = ensure_scene()
            sc.narration = (sc.narration + " " + content).strip() if sc.narration else content
            pending_blank = False
            continue

        # 无标记：空行后另起一镜，否则并入当前镜的旁白
        if pending_blank and current is not None and current.narration:
            current = Scene(index=len(scenes) + 1)
            scenes.append(current)
        sc = ensure_scene()
        sc.narration = (sc.narration + " " + content).strip() if sc.narration else content
        pending_blank = False

    # 标题行若被当成了第一镜的旁白，去掉它
    if scenes and brief.title and scenes[0].narration.strip() == brief.title:
        scenes.pop(0)
        for i, s in enumerate(scenes, 1):
            s.index = i

    brief.scenes = [s for s in scenes if s.narration or s.visual]
    for i, s in enumerate(brief.scenes, 1):
        s.index = i
    return brief


# ---------------- 能力推断与工具选型 ----------------

def _available_by_capability(capability: str) -> list:
    """当前可用的该能力工具，排除 *_selector 这类元工具。

    选择器本身会再委派给某个提供商，它的输入是「透传」语义，具体哪个键
    对应到下游哪个参数并不确定。派单需要能准确构造输入，所以直接选具体
    提供商，选型逻辑由本模块的评分承担。
    """
    registry.ensure_discovered()
    from studio.produce import register as _register_local
    _register_local()
    out = []
    for name in registry.list_all():
        tool = registry.get(name)
        if tool is None or tool.capability != capability:
            continue
        if name.endswith("_selector"):
            continue
        if tool.get_status().value in ("available", "degraded"):
            out.append(tool)
    return out


def _pick(capability: str, prompt: str, budget: Optional[float] = None,
          prefer: tuple[str, ...] = ()) -> dict[str, Any]:
    """在该能力下选出当前可用的最佳工具，附评分与理由。

    `prefer` 用于阶段语义强于能力分类的场合。例如「合成」阶段属于
    video_post，但该分类里还有裁剪、调速、绿幕等工具，盲排会选错；
    此时按 prefer 顺序取第一个可用的即可。
    """
    candidates = _available_by_capability(capability)

    if prefer:
        by_name = {t.name: t for t in candidates}
        for name in prefer:
            if name in by_name:
                return {
                    "capability": capability,
                    "tool": name,
                    "tool_label": i18n.tool_name(name),
                    "available": True,
                    "score": None,
                    "reason": "该阶段的专用工具",
                    "candidates": [
                        {"tool": n, "label": i18n.tool_name(n), "score": None}
                        for n in prefer if n in by_name
                    ],
                }

    if not candidates:
        return {
            "capability": capability,
            "tool": "", "available": False,
            "reason": "当前没有任何可用工具，需要配置对应的 API 密钥或本地依赖",
            "candidates": [],
        }
    ctx = normalize_task_context({"budget_usd": budget} if budget else {},
                                 prompt=prompt, capability=capability)
    ranked = rank_providers(candidates, ctx)
    best = ranked[0]
    dims = [
        ("任务匹配度", best.task_fit), ("输出质量", best.output_quality),
        ("可控性", best.control), ("可靠性", best.reliability),
        ("性价比", best.cost_efficiency), ("速度", best.latency),
    ]
    top_dims = sorted(dims, key=lambda d: -d[1])[:2]
    return {
        "capability": capability,
        "tool": best.tool_name,
        "tool_label": i18n.tool_name(best.tool_name),
        "available": True,
        "score": round(best.weighted_score, 3),
        "reason": "综合评分最高（" + "、".join(f"{n} {v:.2f}" for n, v in top_dims) + "）",
        "candidates": [
            {"tool": r.tool_name, "label": i18n.tool_name(r.tool_name),
             "score": round(r.weighted_score, 3)}
            for r in ranked[:5]
        ],
    }


def _one_shot_available() -> bool:
    """一键成片工具是否可用。"""
    registry.ensure_discovered()
    from studio.produce import register as _register_local
    _register_local()
    tool = registry.get("zero_key_video")
    return tool is not None and tool.get_status().value in ("available", "degraded")


def build_plan(brief: Brief, budget: Optional[float] = None,
               want_subtitle: bool = True, ai_fallback: str = "off") -> dict[str, Any]:
    """根据分镜表推断需要哪些能力，并编排成工序链。

    优先走「一键成片」：zero_key_video 内部已经把配音 → 素材 → 排布 →
    拼接 → 渲染串成一条链，产出的是成片 MP4。逐个工具下发只能得到零散
    素材，字幕和合成还得人工再触发一次，对「贴脚本就要片子」的用法不合适。
    """
    prompt = " ".join(filter(None, [brief.title, brief.style,
                                    " ".join(s.visual for s in brief.scenes)]))[:900]
    has_narration = any(s.narration for s in brief.scenes)
    has_visual = any(s.visual for s in brief.scenes)

    if _one_shot_available():
        return _build_oneshot_plan(brief, has_narration, has_visual, ai_fallback, budget)

    stages: list[dict[str, Any]] = []

    if has_narration:
        pick = _pick("tts", brief.narration_text[:900], budget)
        pick.update({
            "stage": "配音",
            "detail": f"为 {sum(1 for s in brief.scenes if s.narration)} 条旁白生成语音",
            "job_count": sum(1 for s in brief.scenes if s.narration),
            "dispatchable": True,
        })
        stages.append(pick)

    if has_visual:
        # 有本地/免费的检索型工具时优先走检索，否则用生成
        image_pick = _pick("image_generation", prompt, budget)
        stages.append({**image_pick, "stage": "画面素材",
                       "detail": f"为 {sum(1 for s in brief.scenes if s.visual)} 个镜头准备画面",
                       "job_count": sum(1 for s in brief.scenes if s.visual),
                       "dispatchable": True})

    if has_narration and want_subtitle:
        pick = _pick("subtitle", "字幕", budget, prefer=("subtitle_gen", "remotion_caption_burn"))
        stages.append({**pick, "stage": "字幕", "detail": "需要配音产物的时间轴，配音完成后触发",
                       "job_count": 1, "dispatchable": False})

    compose = _pick("video_post", prompt, budget,
                    prefer=("video_compose", "hyperframes_compose", "video_stitch"))
    stages.append({**compose, "stage": "合成", "detail": "需要前序全部产物的真实路径，末位触发",
                   "job_count": 1, "dispatchable": False})

    blocked = [s for s in stages if not s["available"]]
    auto = [s for s in stages if s["available"] and s.get("dispatchable")]
    return {
        "brief": brief.as_dict(),
        "stages": stages,
        "runnable": bool(auto),
        "blocked_stages": [s["stage"] for s in blocked],
        # 只统计本次能直接下发的任务，避免和实际入队数对不上
        "total_jobs": sum(s.get("job_count", 0) for s in auto),
        "deferred_stages": [s["stage"] for s in stages
                            if s["available"] and not s.get("dispatchable")],
    }


def _footage_provider() -> tuple[str, str]:
    """看素材从哪来 —— 有 Pexels 密钥就是策展库，否则是公共档案馆。"""
    try:
        from tools.video.stock_sources import available_sources
        names = {getattr(s, "name", "") for s in available_sources()}
    except Exception:
        names = set()
    if "pexels" in names:
        return "Pexels 策展素材库", "画面质量高、匹配准"
    if names:
        return "公共档案馆（" + "、".join(sorted(names)[:3]) + " 等）", \
               "免费但为关键词匹配，命中质量参差；配 PEXELS_API_KEY 可显著改善"
    return "无可用素材源", "将退回纯文字动画画面"


def _build_oneshot_plan(brief: Brief, has_narration: bool, has_visual: bool,
                        ai_fallback: str = "off",
                        budget: Optional[float] = None) -> dict[str, Any]:
    """一键成片：工序链只做展示，实际下发一个合成任务。"""
    scene_n = len(brief.scenes)
    narr_n = sum(1 for s in brief.scenes if s.narration)
    vis_n = sum(1 for s in brief.scenes if s.visual)
    src_name, src_note = _footage_provider()

    ai_fallback = (ai_fallback or "off").lower()
    if ai_fallback in ("image", "video"):
        unit = 0.04 if ai_fallback == "image" else 0.5
        kind = "图像" if ai_fallback == "image" else "视频"
        est = f"最坏情况 {vis_n} 镜全部生成约 ${vis_n * unit:.2f}"
        cap = f"，预算上限 ${float(budget):.2f}" if budget else "，未设上限"
        src_note += f"；检索没命中时用 AI 生成{kind}兜底（约 ${unit}/段，{est}{cap}）"

    # 这些是 zero_key_video 内部会依次做的事，列出来是为了让你看清链路，
    # 它们不会各自入队 —— 全部在同一个任务里完成。
    inner = [
        {"stage": "配音", "tool": "piper_tts", "tool_label": i18n.tool_name("piper_tts"),
         "available": True, "reason": "本地离线，按脚本语言自动选中英文音色",
         "detail": f"{narr_n} 条旁白逐镜生成", "job_count": 0, "dispatchable": False,
         "candidates": []},
        # 「有没有可用工具」和「脚本里有没有画面建议」是两回事。素材源就绪
        # 但脚本没写画面建议时，标成「受阻」会让人以为要去配密钥 —— 实际
        # 只要在脚本里加一行「画面：」就行，提示必须说清这一点。
        {"stage": "画面素材", "tool": "",
         "tool_label": src_name if has_visual else "文字动画画面",
         "available": True,
         "reason": src_note if has_visual else "脚本未提供画面建议，本次用文字动画呈现",
         "detail": f"按 {vis_n} 条画面建议检索并下载实拍素材" if has_visual
                   else "想要实拍画面，在每镜下加一行「画面：想要的镜头」即可",
         "job_count": 0, "dispatchable": False, "candidates": []},
        {"stage": "排布与拼接", "tool": "ffmpeg", "tool_label": "FFmpeg",
         "available": True, "reason": "按每镜旁白的真实时长排布，声画自动对齐",
         "detail": "拼接旁白音轨、计算每镜时长", "job_count": 0,
         "dispatchable": False, "candidates": []},
        {"stage": "渲染成片", "tool": "zero_key_video",
         "tool_label": i18n.tool_name("zero_key_video") or "零密钥出片",
         "available": True, "reason": "Remotion 渲染 1920×1080 H.264 + AAC",
         "detail": "旁白文字叠加、片尾素材署名、输出 MP4", "job_count": 1,
         "dispatchable": True, "candidates": []},
    ]

    return {
        "brief": brief.as_dict(),
        "stages": inner,
        "runnable": scene_n > 0,
        "blocked_stages": [],
        "total_jobs": 1,
        "deferred_stages": [],
        "oneshot": True,
        "note": f"共 {scene_n} 个分镜，一个任务直接产出成片",
        # 交给 plan_to_jobs 拼进任务输入
        "ai_fallback": ai_fallback,
        "budget_usd": budget,
    }


def plan_to_jobs(brief: Brief, plan: dict[str, Any]) -> list[dict[str, Any]]:
    """把工序链展开成具体的任务列表（tool + inputs）。

    只展开能确定性下发的阶段：配音按镜逐条、画面素材按镜逐条。字幕与合成
    依赖前序产物的真实路径，留给用户在队列里看到产物后再触发。
    """
    jobs: list[dict[str, Any]] = []

    if plan.get("oneshot"):
        job_inputs: dict[str, Any] = {"brief": brief.raw_text, "title": brief.title}
        ai_fallback = plan.get("ai_fallback") or "off"
        if ai_fallback in ("image", "video"):
            job_inputs["ai_fallback"] = ai_fallback
            if plan.get("budget_usd"):
                job_inputs["budget_usd"] = float(plan["budget_usd"])
        return [{
            "tool": "zero_key_video",
            "label": (brief.title or "成片") + f" · {len(brief.scenes)} 镜",
            "inputs": job_inputs,
        }]

    by_stage = {s["stage"]: s for s in plan["stages"]}

    tts = by_stage.get("配音")
    if tts and tts.get("available"):
        for sc in brief.scenes:
            if not sc.narration:
                continue
            jobs.append({
                "tool": tts["tool"],
                "label": f"配音 · 第{sc.index}镜",
                "inputs": {"text": sc.narration},
            })

    visual = by_stage.get("画面素材")
    if visual and visual.get("available"):
        for sc in brief.scenes:
            if not sc.visual:
                continue
            jobs.append({
                "tool": visual["tool"],
                "label": f"画面 · 第{sc.index}镜",
                "inputs": {"prompt": sc.visual},
            })

    return jobs
