"""Simplified-Chinese labels for the console.

Brand names (Kling, Sora, Veo, Runway, ElevenLabs, Pexels…) and established
technical terms (TTS, CLIP, SRT, FFmpeg, Remotion, HyperFrames, LUT) stay in
their original form — translating them would make the tool harder to use, not
easier.
"""

from __future__ import annotations

import re

# ---- 工具中文名 ----
TOOL_NAMES: dict[str, str] = {
    # 角色动画
    "action_timeline_compiler": "动作时间轴编译",
    "character_animation_reviewer": "角色动画审查",
    "character_rig_renderer": "角色绑定渲染",
    "character_spec_generator": "角色设定生成",
    "pose_library_builder": "姿势库构建",
    "svg_rig_builder": "SVG 骨骼绑定",
    # 分析与理解
    "audio_energy": "音频能量分析",
    "audio_probe": "音频信息探测",
    "azure_stt": "Azure 语音转文字",
    "composition_validator": "合成校验",
    "dashscope_asr": "百炼语音识别",
    "face_tracker": "人脸追踪",
    "frame_sampler": "抽帧采样",
    "scene_detect": "镜头切分检测",
    "transcriber": "本地语音转文字",
    "transcript_fetcher": "字幕抓取",
    "video_analyzer": "视频信息分析",
    "video_understand": "视频内容理解",
    "visual_qa": "画面质量问答",
    # 音频处理
    "audio_enhance": "音频降噪增强",
    "audio_mixer": "音频混合",
    # 剪辑与后期
    "auto_reframe": "智能重构图",
    "green_screen_composite": "绿幕合成",
    "green_screen_processor": "绿幕抠像处理",
    "hyperframes_compose": "HyperFrames 合成",
    "showcase_card": "展示卡片生成",
    "silence_cutter": "静音段剪除",
    "video_compose": "视频总合成",
    "video_stitch": "视频拼接",
    "video_trimmer": "视频裁剪调速",
    # 画质增强
    "bg_remove": "背景移除抠图",
    "color_grade": "色彩调色",
    "eye_enhance": "眼神光增强",
    "face_enhance": "人脸增强",
    "face_restore": "人脸修复",
    "upscale": "分辨率超分",
    # 屏幕录制
    "cap_recorder": "屏幕录制 (cap)",
    "screen_capture_selector": "录屏方案选择",
    "screen_recorder": "屏幕录制",
    # 检索
    "clip_search": "CLIP 语义片段检索",
    "direct_clip_search": "素材直查",
    "corpus_builder": "素材语料构建",
    # 图形
    "code_snippet": "代码片段配图",
    "diagram_gen": "图示生成",
    "math_animate": "数学公式动画",
    # AI 视频生成
    "cogvideo_video": "CogVideo 视频生成",
    "comfyui_video": "ComfyUI 视频生成",
    "gemini_omni_video": "Gemini Omni 视频生成",
    "grok_video": "Grok 视频生成",
    "heygen_video": "HeyGen 视频生成",
    "higgsfield_video": "Higgsfield 视频生成",
    "hunyuan_video": "混元视频生成",
    "jimeng_video": "即梦视频生成",
    "kling_official_video": "可灵视频生成（官方）",
    "kling_video": "可灵视频生成",
    "ltx_video_local": "LTX 本地视频生成",
    "ltx_video_modal": "LTX 云端视频生成",
    "minimax_video": "MiniMax 视频生成",
    "pexels_video": "Pexels 素材视频",
    "pixabay_video": "Pixabay 素材视频",
    "runway_video": "Runway 视频生成",
    "seedance_replicate": "Seedance 视频生成 (Replicate)",
    "seedance_video": "Seedance 视频生成",
    "sora_video": "Sora 视频生成",
    "veo_video": "Veo 视频生成",
    "video_selector": "视频方案智能选择",
    "wan_video": "通义万相视频生成",
    # AI 图像生成
    "comfyui_image": "ComfyUI 图像生成",
    "dashscope_image": "百炼图像生成",
    "flux_image": "FLUX 图像生成",
    "google_imagen": "Google Imagen 图像生成",
    "grok_image": "Grok 图像生成",
    "image_gen": "图像生成（通用）",
    "image_selector": "图像方案智能选择",
    "kling_official_image": "可灵图像生成（官方）",
    "local_diffusion": "本地扩散模型出图",
    "openai_image": "OpenAI 图像生成",
    "pexels_image": "Pexels 图库检索",
    "pixabay_image": "Pixabay 图库检索",
    "recraft_image": "Recraft 图像生成",
    # 语音合成
    "dashscope_tts": "百炼语音合成",
    "doubao_tts": "豆包语音合成",
    "elevenlabs_tts": "ElevenLabs 配音",
    "google_tts": "Google 语音合成",
    "kling_tts": "可灵语音合成",
    "openai_tts": "OpenAI 语音合成",
    "piper_tts": "Piper 本地配音（免费离线）",
    "tts_selector": "配音方案智能选择",
    # 音乐
    "freesound_music": "Freesound 音乐检索",
    "pixabay_music": "Pixabay 音乐检索",
    "google_music": "Google Lyria 音乐生成",
    "music_gen": "AI 音乐生成",
    "suno_music": "Suno 音乐生成",
    "music_library": "本地音乐库",
    # 数字人
    "kling_avatar": "可灵数字人",
    "kling_lip_sync": "可灵唇形同步",
    "lip_sync": "唇形同步",
    "talking_head": "口播数字人",
    # 字幕
    "remotion_caption_burn": "Remotion 字幕烧录",
    "subtitle_gen": "字幕生成",
    # 其他
    "zero_key_video": "一键成片（脚本直出）",
    "export_bundle": "成片打包导出",
    "video_downloader": "视频下载",
}

# ---- 表单字段中文名 ----
FIELD_NAMES: dict[str, str] = {
    "text": "文本内容", "prompt": "提示词", "negative_prompt": "反向提示词",
    "input_path": "输入文件", "output_path": "输出路径", "input_paths": "输入文件列表",
    "output_dir": "输出目录", "operation": "操作类型", "model": "模型",
    "voice": "音色", "voice_id": "音色 ID", "speaker_id": "说话人编号",
    "language": "语言", "duration": "时长（秒）", "duration_seconds": "时长（秒）",
    "start_seconds": "起始时间（秒）", "end_seconds": "结束时间（秒）",
    "speed_factor": "速度倍率", "length_scale": "语速系数",
    "sentence_silence": "句间停顿（秒）", "segments": "分段数据",
    "format": "输出格式", "width": "宽度", "height": "高度",
    "output_width": "输出宽度", "output_height": "输出高度",
    "aspect_ratio": "画面比例", "fps": "帧率", "seed": "随机种子",
    "codec": "编码格式", "crf": "画质参数 CRF", "preset": "编码预设",
    "quality": "质量", "style": "风格", "title": "标题", "subtitle": "副标题",
    "background_color": "背景色", "watermark": "水印",
    "audio_path": "音频文件", "video_path": "视频文件", "image_path": "图片文件",
    "subtitle_path": "字幕文件", "script_text": "脚本文本", "script_path": "脚本文件",
    "asset_manifest": "素材清单", "edit_decisions": "剪辑决策表",
    "max_chars_per_line": "每行最大字数", "max_words_per_cue": "每条字幕最大词数",
    "highlight_style": "高亮样式", "corrections": "纠错词表",
    "query": "检索词", "limit": "数量上限", "count": "数量",
    "num_images": "生成张数", "num_frames": "帧数", "steps": "推理步数",
    "guidance_scale": "引导强度", "strength": "强度",
    "reference_image": "参考图", "reference_video": "参考视频",
    "project_dir": "项目目录", "scene_id": "场景编号",
    "options": "高级选项", "profile": "输出配置", "overlays": "叠加层",
    "subtitle_style": "字幕样式", "target_lufs": "目标响度 LUFS",
}

# ---- 界面文案 ----
STATUS_LABELS = {
    "queued": "排队中", "running": "执行中", "success": "成功",
    "failed": "失败", "cancelled": "已取消",
    "available": "可用", "degraded": "降级可用", "unavailable": "待解锁",
}

# ---- best_for / not_good_for 常见英文片段的中文替换 ----
PHRASE_MAP = [
    ("offline narration fallback", "离线配音兜底"),
    ("privacy-sensitive local-only workflows", "隐私敏感的纯本地流程"),
    ("photorealistic", "写实照片级"),
    ("text-to-video", "文生视频"),
    ("image-to-video", "图生视频"),
    ("text-to-image", "文生图"),
    ("stock footage", "库存素材"),
    ("motion graphics", "动态图形"),
    ("kinetic typography", "动态排版"),
    ("talking head", "口播出镜"),
    ("b-roll", "空镜素材"),
    ("word-level captions", "词级字幕"),
    ("social media", "社交媒体"),
    ("product demo", "产品演示"),
    ("high quality", "高质量"),
    ("fast", "快速"),
    ("cheap", "低成本"),
    ("free", "免费"),
    ("local", "本地"),
    ("cloud", "云端"),
]


def localize_dependency_error(msg: str) -> str:
    """把 base_tool 抛出的英文依赖报错换成中文说明，保留命令与包名。"""
    if not msg:
        return ""
    m = re.match(r"Python module '([^']+)' not installed\.?\s*(.*)", msg, re.S)
    if m:
        tail = m.group(2).strip().split("\n")[0]
        return f"缺少 Python 模块 {m.group(1)}" + (f"，安装方式：{tail}" if tail else "")
    m = re.match(r"Command '([^']+)' not found\.?\s*(.*)", msg, re.S)
    if m:
        tail = m.group(2).strip().split("\n")[0]
        return f"找不到命令 {m.group(1)}" + (f"，安装方式：{tail}" if tail else "")
    m = re.match(r"Environment variable '([^']+)' not set\.?\s*(.*)", msg, re.S)
    if m:
        return f"未设置环境变量 {m.group(1)}，请到「密钥配置」填入"
    return msg


def tool_name(tool_id: str) -> str:
    """Chinese display name; falls back to the id for unmapped tools."""
    return TOOL_NAMES.get(tool_id, tool_id)


def field_name(key: str) -> str:
    return FIELD_NAMES.get(key, key)


def localize_phrase(text: str) -> str:
    """Best-effort Chinese rendering of short English capability blurbs."""
    if not text:
        return ""
    lowered = text.lower()
    for en, zh in PHRASE_MAP:
        if en in lowered:
            lowered = lowered.replace(en, zh)
    # Only return the substituted form when it actually changed something,
    # otherwise keep the original casing.
    return lowered if lowered != text.lower() else text


def payload() -> dict:
    """Everything the frontend needs in one blob."""
    return {
        "tools": TOOL_NAMES,
        "fields": FIELD_NAMES,
        "status": STATUS_LABELS,
    }
