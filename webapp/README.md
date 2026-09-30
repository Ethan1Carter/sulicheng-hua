# 溯理成画 Web · AI 定理讲解视频生成系统

给 TheoremExplainAgent 套的一层本地网页界面：在浏览器里输入定理，后台跑原来的
`generate_video.py` 流程，页面上实时显示分镜进度和运行日志，完成后直接播放、下载视频。

## 启动

```powershell
powershell -ExecutionPolicy Bypass -File webapp\start.ps1
```

或者手动启动：

```powershell
$env:PYTHONPATH = (Get-Location)
$env:PYTHONUTF8 = "1"
.\.venv\Scripts\python.exe -m uvicorn webapp.app:app --host 127.0.0.1 --port 8765
```

然后打开 http://127.0.0.1:8765 。端口可以用环境变量 `TEA_WEB_PORT` 改，
默认用 8765 是为了避开常见的 8000 端口占用。

## 前置条件

- `.venv` 里已装好 manim / litellm / kokoro-onnx / fastapi 等依赖
- `tools/ffmpeg.exe`、`tools/ffprobe.exe`（合并视频用，已放在仓库里）
- `models/kokoro-v0_19.onnx`（语音合成模型，约 310 MB）
- 至少一个模型 API Key，在页面右上角「设置」里填
- LaTeX（Manim 渲染公式需要，本机已装 MiKTeX）

## 页面能做什么

- **新建任务**：填定理名称和一句话背景，选模型与画质，点开始生成。
  勾上「只生成分镜脚本」则只跑规划阶段（几十秒），适合快速验证链路。
- **实时进度**：分镜总数、已完成数、当前阶段、已运行时长
- **运行日志**：完整输出，出错时能直接看到是哪一幕报了什么错
- **结果播放**：成片在线播放和下载，下面每个分镜也可以单独点开看
- **部分成功也有结果**：只要有分镜渲染成功，就会拼一个可用版本并标注「部分分镜」
- **历史任务**：记录在 `webapp/jobs/*.json`，日志是同名的 `.log`

## 目录说明

- `webapp/app.py`：FastAPI 后端与任务队列
- `webapp/start.ps1`：启动脚本
- `webapp/static/`：前端（原生 HTML/CSS/JS，无构建步骤）
- `webapp/jobs/`：任务元数据与日志
- `output/web/`：每个任务的渲染产物

## 说明与限制

- 任务串行执行。Manim 渲染很吃 CPU，并发跑只会互相拖慢。
- 不同模型写 Manim 代码的水平差距很大。实测 DeepSeek 类模型经常写错 API，
  同一幕重试多次仍可能失败；GPT-4o / o3-mini / Gemini 这类模型成功率高得多
  （原论文用的就是这些模型）。
- 服务只监听 `127.0.0.1`，供本机使用，不要直接暴露到公网。
