"""Local web interface for TheoremExplainAgent.

Turns the command-line pipeline (``generate_video.py``) into a small website:
type a topic, watch the pipeline work, then play or download the explainer video
it produced.

Run it with:

    .\\.venv\\Scripts\\python.exe -m uvicorn webapp.app:app --host 127.0.0.1 --port 8000

or simply ``powershell -File webapp\\start.ps1``.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
WEBAPP_DIR = Path(__file__).resolve().parent
JOBS_DIR = WEBAPP_DIR / "jobs"
JOBS_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_ROOT = ROOT / "output" / "web"
OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

TOOLS_DIR = ROOT / "tools"
FFMPEG = TOOLS_DIR / "ffmpeg.exe"
FFPROBE = TOOLS_DIR / "ffprobe.exe"
ALLOWED_MODELS_PATH = ROOT / "src" / "utils" / "allowed_models.json"
ENV_PATH = ROOT / ".env"

VENV_BIN = ROOT / ".venv" / ("Scripts" if os.name == "nt" else "bin")
PYTHON = VENV_BIN / ("python.exe" if os.name == "nt" else "python")

QUALITY_CHOICES = {
    "-ql": "480p15 (最快，草稿)",
    "-qm": "720p30 (推荐)",
    "-qh": "1080p60 (最清晰，最慢)",
}

KEY_ENV_NAMES = {
    "deepseek": "DEEPSEEK_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
}

# One-click demo topics that render well and read cleanly in Chinese narration.
# Each fills the topic + a one-line English context (context stays English so the
# on-screen LaTeX never breaks).
EXAMPLE_THEOREMS = [
    {
        "topic": "The Pythagorean Theorem",
        "context": "A fundamental relation in Euclidean geometry among the three sides of a right triangle",
    },
    {
        "topic": "Bayes' Theorem",
        "context": "How to update the probability of a hypothesis given new evidence",
    },
    {
        "topic": "The Fourier Series",
        "context": "Decomposing a periodic function into a sum of sines and cosines",
    },
    {
        "topic": "The Fundamental Theorem of Calculus",
        "context": "The link between differentiation and integration of a function",
    },
    {
        "topic": "Euler's Formula",
        "context": "The identity e^{i x} = cos x + i sin x connecting exponentials and trigonometry",
    },
]


def _edge_tts_available() -> bool:
    """True if the edge-tts package can be imported (Chinese neural voiceover)."""
    import importlib.util

    return importlib.util.find_spec("edge_tts") is not None

# Model-provider failures worth surfacing directly in the UI instead of leaving
# the user to dig through the log.
API_ERROR_HINTS = [
    ("insufficient balance", "模型账户余额不足（返回：Insufficient Balance）。充值或换一个 Key 后重试。"),
    ("insufficient_quota", "模型账户额度不足（insufficient_quota）。"),
    ("invalid_api_key", "API Key 无效，请到「设置」里检查。"),
    ("authenticationerror", "API Key 校验失败，请到「设置」里检查。"),
    ("ratelimiterror", "触发了模型限流，稍后重试。"),
    ("context_length", "提示词超出模型上下文长度。"),
]


def api_error_hint(line: str) -> Optional[str]:
    lowered = line.lower()
    for needle, message in API_ERROR_HINTS:
        if needle in lowered:
            return message
    return None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def slugify(text: str) -> str:
    """Mirror the folder naming used by generate_video.py."""
    return re.sub(r"[^a-z0-9_]+", "_", text.lower())


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def read_allowed_models() -> list[str]:
    try:
        data = json.loads(ALLOWED_MODELS_PATH.read_text(encoding="utf-8"))
        return list(data.get("allowed_models", []))
    except Exception:
        return []


def read_env_file() -> dict[str, str]:
    values: dict[str, str] = {}
    if not ENV_PATH.exists():
        return values
    for raw in ENV_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"')
    return values


def update_env_file(updates: dict[str, str]) -> None:
    """Write/refresh ``KEY="value"`` lines in .env, keeping everything else."""
    lines: list[str] = []
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text(encoding="utf-8", errors="replace").splitlines()

    pending = dict(updates)
    out: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        key = stripped.split("=", 1)[0].strip() if "=" in stripped else None
        if key and key in pending:
            out.append(f'{key}="{pending.pop(key)}"')
        else:
            out.append(raw)
    for key, value in pending.items():
        out.append(f'{key}="{value}"')
    ENV_PATH.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")


def subprocess_env(quality: str, language: str = "zh") -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["TEA_MANIM_QUALITY"] = quality
    # Drives narration language: "zh" -> Chinese voiceover/subtitles via edge-tts,
    # anything else -> English via Kokoro (upstream default).
    env["TEA_NARRATION_LANG"] = language or "zh"
    env["PATH"] = os.pathsep.join(
        [str(VENV_BIN), str(TOOLS_DIR), env.get("PATH", "")]
    )
    return env


def video_duration(path: Path) -> float:
    """Duration in seconds via ffprobe (0.0 when unknown)."""
    if not FFPROBE.exists():
        return 0.0
    try:
        out = subprocess.run(
            [
                str(FFPROBE),
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return float(out.stdout.strip() or 0.0)
    except Exception:
        return 0.0


def has_audio_stream(path: Path) -> bool:
    if not FFPROBE.exists():
        return False
    try:
        out = subprocess.run(
            [
                str(FFPROBE),
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_entries",
                "stream=index",
                "-of",
                "csv=p=0",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return bool(out.stdout.strip())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# job model
# ---------------------------------------------------------------------------


@dataclass
class Job:
    id: str
    topic: str
    context: str
    model: str
    helper_model: str
    quality: str
    max_retries: int
    only_plan: bool
    language: str = "zh"           # zh -> Chinese narration/subtitles, else English
    status: str = "queued"          # queued | running | succeeded | failed | cancelled
    stage: str = "排队中"
    progress: float = 0.0
    created_at: str = field(default_factory=now_iso)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error: Optional[str] = None
    scenes_total: int = 0
    scenes_done: int = 0
    scenes_failed: int = 0
    scene_states: list[dict] = field(default_factory=list)
    video_path: Optional[str] = None
    partial: bool = False
    exit_code: Optional[int] = None

    @property
    def slug(self) -> str:
        return slugify(self.topic)

    @property
    def output_dir(self) -> Path:
        return OUTPUT_ROOT / f"{self.slug}-{self.id}"

    @property
    def topic_dir(self) -> Path:
        return self.output_dir / self.slug

    @property
    def log_path(self) -> Path:
        return JOBS_DIR / f"{self.id}.log"

    @property
    def meta_path(self) -> Path:
        return JOBS_DIR / f"{self.id}.json"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["slug"] = self.slug
        data["video_url"] = f"/api/jobs/{self.id}/video" if self.video_path else None
        data["download_url"] = (
            f"/api/jobs/{self.id}/download" if self.video_path else None
        )
        data["subtitle_url"] = (
            f"/api/jobs/{self.id}/subtitles" if self._subtitle_path() else None
        )
        return data

    def _subtitle_path(self) -> Optional[Path]:
        srt = self.topic_dir / f"{self.slug}_combined.srt"
        return srt if srt.exists() else None

    def save(self) -> None:
        self.meta_path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )


# ---------------------------------------------------------------------------
# job manager
# ---------------------------------------------------------------------------


class JobManager:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.order: list[str] = []
        self.queue: "queue.Queue[str]" = queue.Queue()
        self.current_id: Optional[str] = None
        self.process: Optional[subprocess.Popen] = None
        self.lock = threading.RLock()
        self._load_existing()
        threading.Thread(target=self._worker, daemon=True).start()

    # -- persistence --------------------------------------------------------
    def _load_existing(self) -> None:
        for meta in sorted(JOBS_DIR.glob("*.json")):
            try:
                raw = json.loads(meta.read_text(encoding="utf-8"))
            except Exception:
                continue
            raw.pop("slug", None)
            raw.pop("video_url", None)
            raw.pop("download_url", None)
            raw.pop("subtitle_url", None)
            job = Job(**{k: v for k, v in raw.items() if k in Job.__dataclass_fields__})
            if job.status in {"queued", "running"}:
                # The server was restarted while this job was in flight.
                job.status = "failed"
                job.error = "服务重启，任务中断"
                job.save()
            self.jobs[job.id] = job
            self.order.append(job.id)
        self.order.reverse()

    # -- queueing -----------------------------------------------------------
    def submit(self, payload: "JobRequest") -> Job:
        job_id = uuid.uuid4().hex[:8]
        model = payload.model or read_allowed_models()[0]
        job = Job(
            id=job_id,
            topic=payload.topic.strip(),
            context=payload.context.strip(),
            model=model,
            helper_model=payload.helper_model or model,
            quality=payload.quality if payload.quality in QUALITY_CHOICES else "-qm",
            max_retries=int(payload.max_retries),
            only_plan=bool(payload.only_plan),
            language="zh" if (payload.language or "zh").lower().startswith("zh") else "en",
        )
        with self.lock:
            self.jobs[job.id] = job
            self.order.insert(0, job.id)
        job.log_path.write_text("", encoding="utf-8")
        job.output_dir.mkdir(parents=True, exist_ok=True)
        job.save()
        self.queue.put(job.id)
        return job

    def get(self, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def list_jobs(self) -> list[dict]:
        return [self.jobs[jid].to_dict() for jid in self.order if jid in self.jobs]

    def remove(self, job_id: str) -> None:
        with self.lock:
            self.jobs.pop(job_id, None)
            if job_id in self.order:
                self.order.remove(job_id)
        for path in (JOBS_DIR / f"{job_id}.json", JOBS_DIR / f"{job_id}.log"):
            path.unlink(missing_ok=True)

    def cancel(self, job_id: str) -> Job:
        job = self.get(job_id)
        with self.lock:
            proc = self.process if self.current_id == job_id else None
        if proc and proc.poll() is None:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                )
            else:
                proc.terminate()
            job.status = "cancelled"
            job.stage = "已取消"
        elif job.status == "queued":
            job.status = "cancelled"
            job.stage = "已取消"
            job.save()
        return job

    def retry(self, job_id: str) -> Job:
        """Re-run a finished job. Scenes that already rendered are reused."""
        job = self.get(job_id)
        with self.lock:
            if self.current_id == job_id:
                raise RuntimeError("任务正在运行中")
        job.status = "queued"
        job.stage = "排队中（只重跑失败的分镜）"
        job.error = None
        job.progress = 0.0
        job.started_at = None
        job.finished_at = None
        job.exit_code = None
        job.partial = False
        job.video_path = None
        with open(job.log_path, "a", encoding="utf-8", errors="replace") as handle:
            handle.write("\n[webapp] ==== 重试：已成功的分镜会跳过 ====\n")
        job.save()
        self.queue.put(job.id)
        return job

    # -- worker -------------------------------------------------------------
    def _worker(self) -> None:
        while True:
            job_id = self.queue.get()
            job = self.jobs.get(job_id)
            if job is None or job.status == "cancelled":
                continue
            try:
                self._run(job)
            except Exception as exc:  # pragma: no cover - defensive
                self._log(job, f"[webapp] 任务异常: {exc!r}")
                job.status = "failed"
                job.error = repr(exc)
                job.finished_at = now_iso()
                job.save()

    def _log(self, job: Job, line: str) -> None:
        with open(job.log_path, "a", encoding="utf-8", errors="replace") as handle:
            handle.write(line.rstrip("\n") + "\n")

    def _run(self, job: Job) -> None:
        with self.lock:
            self.current_id = job.id
        job.status = "running"
        job.started_at = now_iso()
        job.stage = "启动生成流程"
        job.save()

        cmd = [
            str(PYTHON),
            "-u",
            str(ROOT / "generate_video.py"),
            "--model",
            job.model,
            "--helper_model",
            job.helper_model,
            "--output_dir",
            str(job.output_dir),
            "--topic",
            job.topic,
            "--context",
            job.context,
            "--max_retries",
            str(job.max_retries),
        ]
        if job.only_plan:
            cmd.append("--only_plan")

        self._log(job, f"[webapp] $ {' '.join(cmd)}")
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            env=subprocess_env(job.quality, job.language),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        with self.lock:
            self.process = proc

        assert proc.stdout is not None
        for line in proc.stdout:
            self._log(job, line)
            hint = api_error_hint(line)
            if hint:
                job.error = hint
            self.refresh_progress(job)
        proc.wait()

        with self.lock:
            self.process = None
            self.current_id = None
        job.exit_code = proc.returncode
        job.finished_at = now_iso()

        if job.status == "cancelled":
            job.save()
            return

        if job.only_plan:
            job.status = "succeeded" if proc.returncode == 0 else "failed"
            job.stage = "规划完成（未渲染视频）"
            job.progress = 100.0
            job.save()
            return

        self._collect_result(job)
        job.save()

    # -- progress -----------------------------------------------------------
    def refresh_progress(self, job: Job) -> None:
        """Recompute stage/progress from the pipeline's own output files."""
        outline = job.topic_dir / f"{job.slug}_scene_outline.txt"
        scenes_total = 0
        if outline.exists():
            try:
                content = outline.read_text(encoding="utf-8", errors="replace")
                scenes_total = len(re.findall(r"<SCENE_(\d+)>[^<]", content))
            except Exception:
                scenes_total = 0

        states: list[dict] = []
        done = 0
        for number in range(1, scenes_total + 1):
            scene_dir = job.topic_dir / f"scene{number}"
            ok = (scene_dir / "succ_rendered.txt").exists()
            code_dir = scene_dir / "code"
            attempts = (
                len(list(code_dir.glob("*.py"))) if code_dir.exists() else 0
            )
            states.append({"scene": number, "done": ok, "attempts": attempts})
            done += 1 if ok else 0

        job.scenes_total = scenes_total
        job.scenes_done = done
        job.scene_states = states

        combined = job.topic_dir / f"{job.slug}_combined.mp4"
        if combined.exists():
            job.video_path = str(combined)

        if job.status != "running":
            return

        if combined.exists():
            job.stage = "合并完成"
            job.progress = 99.0
        elif scenes_total and done >= scenes_total:
            job.stage = "合并分镜视频"
            job.progress = 95.0
        elif scenes_total and done:
            job.stage = f"渲染分镜 {done}/{scenes_total}"
            job.progress = 20.0 + 70.0 * done / max(scenes_total, 1)
        elif scenes_total:
            # Count finished per-scene planning documents so the planning phase
            # shows movement instead of sitting at a fixed 20%.
            plans_done = len(
                list(job.topic_dir.glob("scene*/subplans/*_technical_implementation_plan.txt"))
            )
            plans_done = min(plans_done, scenes_total)
            job.stage = f"生成分镜脚本 {plans_done}/{scenes_total}"
            job.progress = 5.0 + 15.0 * plans_done / max(scenes_total, 1)
        elif outline.exists():
            job.stage = "解析分镜脚本"
            job.progress = 15.0
        else:
            job.stage = "规划分镜脚本"
            job.progress = max(job.progress, 5.0)
        job.save()

    # -- results ------------------------------------------------------------
    def _scene_videos(self, job: Job) -> list[tuple[int, Path]]:
        """Latest rendered mp4 per scene, ordered by scene number."""
        media = job.topic_dir / "media" / "videos"
        if not media.exists():
            return []
        best: dict[int, tuple[int, Path]] = {}
        pattern = re.compile(rf"^{re.escape(job.slug)}_scene(\d+)_v(\d+)$")
        for folder in media.iterdir():
            if not folder.is_dir():
                continue
            match = pattern.match(folder.name)
            if not match:
                continue
            scene_no, version = int(match.group(1)), int(match.group(2))
            clip = None
            for quality_dir in sorted(folder.iterdir(), reverse=True):
                if not quality_dir.is_dir():
                    continue
                mp4s = sorted(quality_dir.glob("*.mp4"))
                if mp4s:
                    clip = mp4s[0]
                    break
            if clip is None:
                continue
            if scene_no not in best or version > best[scene_no][0]:
                best[scene_no] = (version, clip)
        return sorted((no, clip) for no, (_, clip) in best.items())

    def _collect_result(self, job: Job) -> None:
        combined = job.topic_dir / f"{job.slug}_combined.mp4"
        if combined.exists():
            job.video_path = str(combined)
            job.status = "succeeded"
            job.stage = "完成"
            job.progress = 100.0
            return

        clips = self._scene_videos(job)
        if not clips:
            job.status = "failed"
            job.stage = "失败"
            job.error = (
                job.error
                or "没有任何分镜渲染成功。常见原因：模型生成的 Manim 代码有语法/API 错误，或缺少 API Key。详见日志。"
            )
            return

        # The pipeline only merges when *every* scene rendered. If some scenes
        # failed, still give the user a watchable cut of what did work.
        self._log(
            job,
            f"[webapp] 官方合并未产出成片，改用回退合并："
            f"{len(clips)} 个分镜（{job.scenes_done}/{job.scenes_total} 成功）",
        )
        try:
            out = job.topic_dir / f"{job.slug}_partial.mp4"
            self._combine_clips(clips, out)
            job.video_path = str(out)
            job.partial = True
            job.status = "succeeded"
            job.stage = "完成（部分分镜）"
            job.progress = 100.0
            self._log(job, f"[webapp] 回退合并完成: {out.name}")
        except Exception as exc:
            job.status = "failed"
            job.stage = "合并失败"
            job.error = f"合并分镜视频失败: {exc!r}"
            self._log(job, f"[webapp] 合并失败: {exc!r}")

    def _combine_clips(self, clips: list[tuple[int, Path]], output: Path) -> None:
        """Normalise every clip (uniform codec + guaranteed audio) then concat."""
        if not FFMPEG.exists():
            raise RuntimeError("找不到 tools/ffmpeg.exe")

        work = output.parent / "_combine_tmp"
        work.mkdir(parents=True, exist_ok=True)
        normalised: list[Path] = []

        for scene_no, clip in clips:
            target = work / f"scene{scene_no}.mp4"
            self._normalise_clip(clip, target)
            normalised.append(target)

        list_file = work / "concat.txt"
        list_file.write_text(
            "\n".join(f"file '{p.as_posix()}'" for p in normalised) + "\n",
            encoding="utf-8",
        )
        subprocess.run(
            [
                str(FFMPEG),
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_file),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(output),
            ],
            check=True,
            capture_output=True,
        )

    def _normalise_clip(self, source: Path, target: Path) -> None:
        has_audio = has_audio_stream(source)
        duration = video_duration(source)
        cmd = [str(FFMPEG), "-y", "-hide_banner", "-loglevel", "error", "-i", str(source)]
        if not has_audio:
            if duration <= 0:
                duration = 1.0
            cmd += [
                "-f",
                "lavfi",
                "-t",
                f"{duration:.3f}",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=44100",
            ]
        cmd += [
            "-map",
            "0:v:0",
            "-map",
            ("1:a:0" if not has_audio else "0:a:0"),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-r",
            "30",
            "-c:a",
            "aac",
            "-ar",
            "44100",
            "-ac",
            "2",
            "-shortest",
            "-movflags",
            "+faststart",
            str(target),
        ]
        subprocess.run(cmd, check=True, capture_output=True)


manager = JobManager()


# ---------------------------------------------------------------------------
# API models
# ---------------------------------------------------------------------------


class JobRequest(BaseModel):
    topic: str = Field(min_length=1, max_length=200)
    context: str = Field(default="", max_length=2000)
    model: str = ""
    helper_model: str = ""
    quality: str = "-qm"
    max_retries: int = Field(default=3, ge=1, le=8)
    only_plan: bool = False
    language: str = "zh"


class SettingsRequest(BaseModel):
    deepseek: str | None = None
    openai: str | None = None
    gemini: str | None = None


class ModelRequest(BaseModel):
    model_id: str = Field(min_length=3, max_length=120)


# ---------------------------------------------------------------------------
# app
# ---------------------------------------------------------------------------


app = FastAPI(title="TheoremExplainAgent Web", version="0.1.0")
app.mount("/static", StaticFiles(directory=WEBAPP_DIR / "static"), name="static")


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((WEBAPP_DIR / "static" / "index.html").read_text(encoding="utf-8"))


@app.get("/api/config")
def get_config() -> dict:
    env = read_env_file()
    models = read_allowed_models()
    provider_keys = {
        provider: bool(env.get(name) or os.environ.get(name))
        for provider, name in KEY_ENV_NAMES.items()
    }
    return {
        "models": models,
        "qualities": [{"value": k, "label": v} for k, v in QUALITY_CHOICES.items()],
        "default_model": next(
            (m for m in models if m.startswith("deepseek/")), models[0] if models else ""
        ),
        "provider_keys": provider_keys,
        "python_ready": PYTHON.exists(),
        "ffmpeg_ready": FFMPEG.exists(),
        "tts_ready": (ROOT / "models" / "kokoro-v0_19.onnx").exists(),
        "edge_tts_ready": _edge_tts_available(),
        "examples": EXAMPLE_THEOREMS,
    }


@app.post("/api/settings")
def save_settings(payload: SettingsRequest) -> dict:
    updates = {}
    for provider, value in payload.model_dump().items():
        if value is None:
            continue
        value = value.strip()
        if value:
            updates[KEY_ENV_NAMES[provider]] = value
    if updates:
        update_env_file(updates)
    return get_config()


@app.post("/api/models")
def add_model(payload: ModelRequest) -> dict:
    model_id = payload.model_id.strip()
    data = json.loads(ALLOWED_MODELS_PATH.read_text(encoding="utf-8"))
    models = data.setdefault("allowed_models", [])
    if model_id not in models:
        models.append(model_id)
        ALLOWED_MODELS_PATH.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    return {"models": read_allowed_models()}


@app.get("/api/jobs")
def list_jobs() -> dict:
    return {"jobs": manager.list_jobs()}


@app.post("/api/jobs")
def create_job(payload: JobRequest) -> dict:
    if not PYTHON.exists():
        raise HTTPException(status_code=503, detail="缺少 Python 虚拟环境 .venv")
    job = manager.submit(payload)
    return job.to_dict()


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    try:
        job = manager.get(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="任务不存在")
    if job.status == "running":
        manager.refresh_progress(job)
    return job.to_dict()


@app.get("/api/jobs/{job_id}/log", response_class=PlainTextResponse)
def job_log(job_id: str, tail: int = 400) -> str:
    try:
        job = manager.get(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not job.log_path.exists():
        return ""
    lines = job.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-max(1, min(tail, 5000)):])


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    try:
        return manager.cancel(job_id).to_dict()
    except KeyError:
        raise HTTPException(status_code=404, detail="任务不存在")


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str) -> dict:
    try:
        return manager.retry(job_id).to_dict()
    except KeyError:
        raise HTTPException(status_code=404, detail="任务不存在")
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str) -> dict:
    try:
        manager.remove(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"ok": True}


@app.get("/api/jobs/{job_id}/video")
def job_video(job_id: str):
    job = _job_or_404(job_id)
    path = Path(job.video_path) if job.video_path else None
    if not path or not path.exists():
        raise HTTPException(status_code=404, detail="视频尚未生成")
    return FileResponse(path, media_type="video/mp4", filename=path.name)


@app.get("/api/jobs/{job_id}/download")
def job_download(job_id: str):
    job = _job_or_404(job_id)
    path = Path(job.video_path) if job.video_path else None
    if not path or not path.exists():
        raise HTTPException(status_code=404, detail="视频尚未生成")
    return FileResponse(path, media_type="video/mp4", filename=path.name)


@app.get("/api/jobs/{job_id}/subtitles")
def job_subtitles(job_id: str):
    job = _job_or_404(job_id)
    srt = job.topic_dir / f"{job.slug}_combined.srt"
    if not srt.exists():
        raise HTTPException(status_code=404, detail="字幕尚未生成")
    return FileResponse(srt, media_type="text/plain", filename=srt.name)


@app.get("/api/jobs/{job_id}/scenes")
def job_scenes(job_id: str) -> dict:
    job = _job_or_404(job_id)
    scenes = []
    for number, clip in manager._scene_videos(job):
        scenes.append(
            {
                "scene": number,
                "url": f"/api/jobs/{job_id}/scenes/{number}/video",
                "name": clip.name,
            }
        )
    return {"scenes": scenes}


@app.get("/api/jobs/{job_id}/scenes/{scene_no}/video")
def job_scene_video(job_id: str, scene_no: int):
    job = _job_or_404(job_id)
    for number, clip in manager._scene_videos(job):
        if number == scene_no:
            return FileResponse(clip, media_type="video/mp4", filename=clip.name)
    raise HTTPException(status_code=404, detail="该分镜没有可用视频")


def _job_or_404(job_id: str) -> Job:
    try:
        return manager.get(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="任务不存在")


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "time": time.time()}
