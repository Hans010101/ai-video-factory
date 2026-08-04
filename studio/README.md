# Studio · 本地批量视频生产工作台

一个跑在本机的控制台，用来**直接调用和批量运行**这个项目里的全部 102 个工具。

和 `backlot/` 的区别：Backlot 是只读的故事板监视器（只有 GET 接口）；Studio 是控制面，能下发任务、并发执行、管理密钥。

## 启动

```bash
cd ai-video-factory && nvm use && .venv/bin/python -m studio --open
```

默认 `http://127.0.0.1:8760`，只绑定本地回环 —— 它能执行工具和改写 `.env`，不要暴露到公网。

| 参数 | 默认 | 说明 |
|---|---|---|
| `--port` | 8760 | 端口 |
| `--workers` | 3 | 并发执行的任务数 |
| `--open` | 关 | 启动后自动打开浏览器 |

## 七个面板

**智能派单**（主入口）—— 粘贴脚本 / 提示词 / 画面建议，或直接上传 txt / md / docx / pdf。系统会：

1. **解析分镜表** —— 识别「场景1」「旁白：」「画面：」等标记；也支持完全无标记（按空行分段，每段视为一条旁白）
2. **推断所需能力** —— 有旁白→配音，有画面建议→画面素材，再加字幕与合成
3. **自动匹配工具** —— 用项目自带的 `lib/scoring.py` 七维评分（任务匹配度 / 输出质量 / 可控性 / 可靠性 / 性价比 / 速度 / 连续性）在**当前可用**的工具里选最佳，并给出评分与理由，每道工序都可手动改选备选工具
4. **下发任务** —— 一键批量入队

选型刻意排除了 `*_selector` 这类元工具：它们会再委派给某个提供商，输入是「透传」语义，具体键怎么映射并不确定，派单需要能准确构造输入，所以直接选具体提供商。

配音会**按文本语言自动选音色**（中文旁白用 `zh_CN-huayan-medium`，英文用 `en_US-lessac-medium`）。

字幕与合成两道工序**不会自动下发** —— 它们需要前序产物的真实路径。等配音/画面跑完，到「成片库」拿到文件，再去「单工具」页触发。界面上会如实标注「待前序产物」，下发按钮的数字也只算能真正入队的任务。

**单工具** —— 左侧按能力分组列出全部工具，点任意工具即根据它的 `input_schema` **自动生成表单**（必填校验、枚举下拉、默认值、数值范围）。底部「批量生产」选一个字段作为变量，每行一个值，一次入队 N 个任务；其余参数沿用表单。`output_path` 留空会自动分配到 `projects/studio/<日期>/<工具名>/`。

**队列** —— SSE 实时推送，显示状态、耗时、成本、产出文件直链、失败原因。

**成片库** —— 扫描 `projects/` 下所有产物，视频/图片直接内联预览。

**工具库** —— 102 个工具全景，可筛「只看待解锁」，每个待解锁工具直接标出缺什么。

**密钥** —— 每个密钥标注**能解锁几个工具**并附申请链接。写入本机 `.env`，已保存的值不回传浏览器（只显示掩码），留空保存即清除。保存后立即重算可用性，无需重启。

**环境** —— 命令行依赖、Python 模块体检，以及可免费安装的扩展清单。

## 启动器做的三件关键修复

1. **PATH** —— 把 `.venv/bin` 和 Homebrew 加进 PATH。工具用 `subprocess` 调外部命令，不加的话 `piper` 找不到。
2. **TLS** —— python.org 版 Python 在 macOS 上不带 CA 证书（`ssl.get_default_verify_paths().cafile` 为 `None`），**所有 HTTPS 请求都会失败**。启动器自动指向 certifi 的证书包，修复全部云端工具。
3. **Piper 模型路径** —— `piper_tts` 把 `model` 直接透传给 CLI 且不传 `--data-dir`，裸名称找不到模型。Studio 自动把 `en_US-lessac-medium` 展开为 `~/.piper/models/en_US-lessac-medium.onnx`。

## 语音模型

已下载到 `~/.piper/models/`：

- `en_US-lessac-medium` — 英文
- `zh_CN-huayan-medium` — 中文

装更多：

```bash
.venv/bin/python -m piper.download_voices <voice_name> --download-dir ~/.piper/models
```

## HTTP 接口

控制台就是一层 UI，接口可以直接给脚本调用：

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/catalog?refresh=true` | 全部工具 + schema + 可用性 |
| POST | `/api/jobs` | 单任务 `{tool, inputs, label}` |
| POST | `/api/batch` | 批量 `{tool, rows[], label}` |
| GET | `/api/jobs` | 任务列表 + 统计 |
| GET | `/api/stream` | SSE 实时任务流 |
| GET/POST | `/api/keys` | 密钥状态 / 写入 |
| GET | `/api/doctor` | 环境体检 |
| GET | `/api/outputs` | 产物列表 |

批量的例子：

```bash
curl -X POST http://127.0.0.1:8760/api/batch \
  -H 'Content-Type: application/json' \
  -d '{"tool":"piper_tts","rows":[{"text":"第一条"},{"text":"第二条"}]}'
```

## 边界

Studio 直接调用**单个工具**，适合批量生产素材（配音、图像、片段、字幕、转码、剪辑）。

完整的**端到端流水线**（研究 → 提案 → 脚本 → 场景规划 → 资产 → 剪辑 → 合成）仍由 AI 编程助手编排 —— 那套逻辑写在 `pipeline_defs/*.yaml` 和 `skills/` 的导演技能里，需要在创意决策点做判断，不是代码能替代的。两者互补：用助手做成片，用 Studio 批量产素材和做后期。
