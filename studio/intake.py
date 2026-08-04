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
_SCENE_HEAD = re.compile(
    r"^(?:#{1,6}\s*)?(?:场景|镜头|片段|段落|scene|shot|part)\s*[:：#]?\s*(\d+)?[.、:：]?\s*(.*)$",
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
    """把「旁白：xxx」拆成 (tag, content)；没有标记时 tag 为空。"""
    m = _TAG_LINE.match(line)
    if not m:
        return "", line.strip()
    return m.group(1).strip().lower(), m.group(2).strip()


def parse_brief(text: str) -> Brief:
    """把自由格式的脚本解析成分镜表。

    识别三种写法，混用也能处理：
      1. 「场景 1」「镜头2:」「## 场景」等标题分段
      2. 段落内用「旁白：」「画面：」标注角色
      3. 完全无标记 —— 按空行分段，每段视为一条旁白
    """
    lines = [ln.rstrip() for ln in (text or "").replace("\r\n", "\n").split("\n")]
    brief = Brief(raw_chars=len(text or ""))

    # 标题：第一条非空行，若像 markdown 标题或很短则采用
    for ln in lines:
        if ln.strip():
            candidate = re.sub(r"^#+\s*", "", ln).strip()
            if len(candidate) <= 60 and not _SCENE_HEAD.match(ln):
                brief.title = candidate
            break

    scenes: list[Scene] = []
    current: Optional[Scene] = None
    pending_blank = False

    def ensure_scene() -> Scene:
        nonlocal current
        if current is None:
            current = Scene(index=len(scenes) + 1)
            scenes.append(current)
        return current

    for ln in lines:
        stripped = ln.strip()
        if not stripped:
            pending_blank = True
            continue

        head = _SCENE_HEAD.match(stripped)
        if head and (head.group(1) or head.group(2)):
            current = Scene(index=len(scenes) + 1, title=(head.group(2) or "").strip())
            scenes.append(current)
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
            sc = ensure_scene()
            sc.narration = (sc.narration + " " + content).strip() if sc.narration else content
            pending_blank = False
            continue
        if tag and any(t in tag for t in _VISUAL_TAGS):
            sc = ensure_scene()
            sc.visual = (sc.visual + " " + content).strip() if sc.visual else content
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


def build_plan(brief: Brief, budget: Optional[float] = None,
               want_subtitle: bool = True) -> dict[str, Any]:
    """根据分镜表推断需要哪些能力，并编排成工序链。"""
    prompt = " ".join(filter(None, [brief.title, brief.style,
                                    " ".join(s.visual for s in brief.scenes)]))[:900]
    has_narration = any(s.narration for s in brief.scenes)
    has_visual = any(s.visual for s in brief.scenes)

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


def plan_to_jobs(brief: Brief, plan: dict[str, Any]) -> list[dict[str, Any]]:
    """把工序链展开成具体的任务列表（tool + inputs）。

    只展开能确定性下发的阶段：配音按镜逐条、画面素材按镜逐条。字幕与合成
    依赖前序产物的真实路径，留给用户在队列里看到产物后再触发。
    """
    jobs: list[dict[str, Any]] = []
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
