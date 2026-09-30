const state = {
  config: null,
  jobs: [],
  current: null,       // job object of the selected job
  timer: null,
  pollMs: 2000,
};

const STATUS_LABEL = {
  queued: "排队中",
  running: "生成中",
  succeeded: "完成",
  failed: "失败",
  cancelled: "已取消",
};

const $ = (id) => document.getElementById(id);

async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  const type = res.headers.get("content-type") || "";
  return type.includes("application/json") ? res.json() : res.text();
}

/* ------------------------------------------------------------------ config */

async function loadConfig() {
  state.config = await api("/api/config");
  const { models, qualities, provider_keys, python_ready, ffmpeg_ready, tts_ready, edge_tts_ready, examples } = state.config;

  $("model").innerHTML = models
    .map((m) => `<option value="${m}">${m}</option>`)
    .join("");
  $("model").value = state.config.default_model || models[0] || "";

  $("quality").innerHTML = qualities
    .map((q) => `<option value="${q.value}">${q.label}</option>`)
    .join("");
  $("quality").value = "-qm";

  // Example-theorem presets (one-click fill).
  const picker = $("example-picker");
  if (picker && Array.isArray(examples)) {
    picker.innerHTML =
      `<option value="">— 自己输入 —</option>` +
      examples
        .map((e, i) => `<option value="${i}">${escapeHtml(e.topic)}</option>`)
        .join("");
  }

  const chips = [
    { label: "Python 环境", ok: python_ready },
    { label: "ffmpeg", ok: ffmpeg_ready },
    { label: "英文配音", ok: tts_ready },
    { label: "中文配音", ok: edge_tts_ready },
    { label: "DeepSeek Key", ok: provider_keys.deepseek },
    { label: "OpenAI Key", ok: provider_keys.openai },
    { label: "Gemini Key", ok: provider_keys.gemini },
  ];
  $("env-chips").innerHTML = chips
    .map(
      (c) =>
        `<span class="chip ${c.ok ? "on" : "off"}">${c.ok ? "✓" : "!"} ${c.label}</span>`
    )
    .join("");

  const missing = [];
  if (!provider_keys.deepseek && !provider_keys.openai && !provider_keys.gemini) {
    missing.push("至少配置一个模型 API Key");
  }
  $("form-hint").textContent = missing.length
    ? `注意：还没有配置 API Key。点右上角「设置」填入后即可开始。`
    : "一次生成通常需要 5–20 分钟（取决于分镜数量与画质）。";
}

/* ---------------------------------------------------------------- job list */

async function loadJobs() {
  const data = await api("/api/jobs");
  state.jobs = data.jobs;
  renderJobList();
}

function renderJobList() {
  const list = $("job-list");
  if (!state.jobs.length) {
    list.innerHTML = `<li class="meta" style="cursor:default">还没有任务</li>`;
    return;
  }
  list.innerHTML = state.jobs
    .map((job) => {
      const active = state.current && state.current.id === job.id ? "active" : "";
      const time = (job.created_at || "").replace("T", " ").slice(5, 16);
      const meta = job.only_plan
        ? "仅规划"
        : `${job.scenes_done || 0}/${job.scenes_total || "?"} 分镜`;
      return `<li class="${active}" data-id="${job.id}">
        <div class="row1">
          <span class="topic">${escapeHtml(job.topic)}</span>
          <span class="time">${time}</span>
        </div>
        <div class="meta">${STATUS_LABEL[job.status] || job.status} · ${meta}</div>
      </li>`;
    })
    .join("");
  list.querySelectorAll("li[data-id]").forEach((li) => {
    li.addEventListener("click", () => selectJob(li.dataset.id));
  });
}

/* -------------------------------------------------------------- job select */

async function selectJob(jobId) {
  state.current = state.jobs.find((j) => j.id === jobId) || null;
  if (!state.current) {
    const job = await api(`/api/jobs/${jobId}`);
    state.current = job;
  }
  $("empty-state").classList.add("hidden");
  $("job-view").classList.remove("hidden");
  $("player").removeAttribute("src");
  $("player-placeholder").classList.remove("hidden");
  renderCurrent();
  schedulePolling();
}

async function refreshCurrent() {
  if (!state.current) return;
  try {
    state.current = await api(`/api/jobs/${state.current.id}`);
    const idx = state.jobs.findIndex((j) => j.id === state.current.id);
    if (idx >= 0) state.jobs[idx] = state.current;
    renderCurrent();
    renderJobList();
    await loadLog();
    await loadScenes();
  } catch (err) {
    console.warn(err);
  }
}

function renderCurrent() {
  const job = state.current;
  if (!job) return;

  $("view-topic").textContent = job.topic;
  const badge = $("view-badge");
  badge.textContent = STATUS_LABEL[job.status] || job.status;
  badge.className = `badge ${job.status}`;

  const pct = Math.max(0, Math.min(100, job.progress || 0));
  $("view-progress").style.width = `${pct}%`;
  $("view-percent").textContent = `${Math.round(pct)}%`;

  const stageBits = [job.stage || "—"];
  if (job.scenes_total) stageBits.push(`${job.scenes_done}/${job.scenes_total} 分镜`);
  const started = job.started_at ? new Date(job.started_at).getTime() : null;
  const ended = job.finished_at ? new Date(job.finished_at).getTime() : null;
  if (started) {
    const seconds = Math.round(((ended || Date.now()) - started) / 1000);
    stageBits.push(`已运行 ${formatDuration(seconds)}`);
  }
  $("view-stage").textContent = stageBits.join(" · ");

  const cancelBtn = $("cancel-btn");
  cancelBtn.classList.toggle("hidden", !(job.status === "running" || job.status === "queued"));

  const retryBtn = $("retry-btn");
  const hasFailedScenes =
    job.scenes_total > 0 && job.scenes_done < job.scenes_total;
  const finished = job.status === "succeeded" || job.status === "failed";
  retryBtn.classList.toggle("hidden", !(finished && hasFailedScenes));
  retryBtn.textContent = hasFailedScenes
    ? `重试未完成的 ${job.scenes_total - job.scenes_done} 个分镜`
    : "重试失败的分镜";

  const errorBox = $("view-error");
  if (job.error) {
    errorBox.textContent = job.error;
    errorBox.classList.remove("hidden");
  } else {
    errorBox.classList.add("hidden");
  }

  const dl = $("view-download");
  if (job.video_url) {
    dl.href = job.video_url;
    dl.classList.remove("hidden");
    if (!$("player").src) {
      $("player").src = job.video_url;
      $("player-placeholder").classList.add("hidden");
    }
    $("player-title").textContent = job.partial ? "成片（部分分镜）" : "成片";
    $("player-note").textContent = job.partial
      ? "有分镜渲染失败，这里是把成功的分镜拼起来的版本。"
      : job.subtitle_url
      ? "含字幕文件"
      : "";
  } else {
    dl.classList.add("hidden");
    $("player-title").textContent = "成片";
    $("player-note").textContent = "";
  }
}

/* -------------------------------------------------------------------- logs */

async function loadLog() {
  if (!state.current) return;
  const text = await api(`/api/jobs/${state.current.id}/log?tail=500`);
  const el = $("log");
  const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 24;
  el.textContent = text || "（暂无输出）";
  if ($("autoscroll").checked && atBottom) el.scrollTop = el.scrollHeight;
}

/* ------------------------------------------------------------------ scenes */

async function loadScenes() {
  if (!state.current) return;
  const job = state.current;
  const strip = $("scene-strip");
  const states = job.scene_states || [];
  if (!states.length) {
    strip.innerHTML = "";
    return;
  }

  let clips = [];
  if (job.status !== "running") {
    try {
      clips = (await api(`/api/jobs/${job.id}/scenes`)).scenes;
    } catch (_) {}
  }
  const clipFor = (n) => clips.find((c) => c.scene === n);

  strip.innerHTML = states
    .map((s) => {
      const cls = s.done ? "done" : "failed";
      const label = s.done ? `分镜 ${s.scene}` : `分镜 ${s.scene}（未成功）`;
      return `<span class="scene-chip ${cls}" data-scene="${s.scene}" data-ok="${s.done}">
        <span class="dot"></span>${label}
      </span>`;
    })
    .join("");

  strip.querySelectorAll(".scene-chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      const n = Number(chip.dataset.scene);
      const clip = clipFor(n);
      if (!clip) return;
      strip.querySelectorAll(".scene-chip").forEach((c) => c.classList.remove("playing"));
      chip.classList.add("playing");
      const player = $("player");
      player.src = clip.url;
      player.load();
      player.play().catch(() => {});
      $("player-placeholder").classList.add("hidden");
      $("player-title").textContent = `第 ${n} 幕`;
      $("player-note").textContent = "（点击后重新生成可回到完整成片）";
    });
  });
}

/* ----------------------------------------------------------------- polling */

function schedulePolling() {
  if (state.timer) clearTimeout(state.timer);
  const active =
    state.current && (state.current.status === "running" || state.current.status === "queued");
  state.timer = setTimeout(async () => {
    if (state.current) await refreshCurrent();
    if (!active) await loadJobs();
    schedulePolling();
  }, active ? state.pollMs : 8000);
}

/* ------------------------------------------------------------------- form */

$("job-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const btn = $("submit-btn");
  btn.disabled = true;
  btn.textContent = "提交中…";
  try {
    const payload = {
      topic: $("topic").value.trim(),
      context: $("context").value.trim(),
      language: $("language").value,
      model: $("model").value,
      helper_model: $("model").value,
      quality: $("quality").value,
      max_retries: Number($("max_retries").value) || 3,
      only_plan: $("only_plan").checked,
    };
    const job = await api("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    state.jobs.unshift(job);
    state.current = job;
    $("empty-state").classList.add("hidden");
    $("job-view").classList.remove("hidden");
    renderJobList();
    renderCurrent();
    schedulePolling();
  } catch (err) {
    alert(`提交失败：${err.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = "开始生成";
  }
});

$("cancel-btn").addEventListener("click", async () => {
  if (!state.current) return;
  if (!confirm("确定要取消这个任务吗？已经渲染出来的分镜会保留。")) return;
  state.current = await api(`/api/jobs/${state.current.id}/cancel`, { method: "POST" });
  renderCurrent();
  renderJobList();
});

$("retry-btn").addEventListener("click", async () => {
  if (!state.current) return;
  try {
    state.current = await api(`/api/jobs/${state.current.id}/retry`, { method: "POST" });
    renderCurrent();
    renderJobList();
    schedulePolling();
  } catch (err) {
    alert(`重试失败：${err.message}`);
  }
});

$("delete-btn").addEventListener("click", async () => {
  if (!state.current) return;
  if (!confirm("只移除这条任务记录，已生成的视频文件会保留在 output 目录里。继续？")) return;
  await api(`/api/jobs/${state.current.id}`, { method: "DELETE" });
  state.current = null;
  $("job-view").classList.add("hidden");
  $("empty-state").classList.remove("hidden");
  await loadJobs();
});

$("refresh-btn").addEventListener("click", loadJobs);

/* ---------------------------------------------------- language hint toggle */

function updateTopicHint() {
  const hint = $("topic-hint");
  if (!hint) return;
  hint.textContent =
    $("language").value === "zh"
      ? "讲解语言选「中文」时，旁白、字幕与画面中的标题/标签均为中文，纯数学公式保持标准 LaTeX。"
      : "英文模式：旁白、字幕、画面标签全部为英文，用英文写主题效果最好。";
}
$("language").addEventListener("change", updateTopicHint);

/* ------------------------------------------------------- example presets */

$("example-picker").addEventListener("change", (event) => {
  const idx = event.target.value;
  if (idx === "") return;
  const example = (state.config && state.config.examples || [])[Number(idx)];
  if (!example) return;
  $("topic").value = example.topic || "";
  $("context").value = example.context || "";
});

/* --------------------------------------------------------------- settings */

$("settings-btn").addEventListener("click", () => {
  $("settings-modal").classList.remove("hidden");
  $("settings-status").textContent = "";
});
$("settings-close").addEventListener("click", () => $("settings-modal").classList.add("hidden"));
$("settings-modal").addEventListener("click", (event) => {
  if (event.target === $("settings-modal")) $("settings-modal").classList.add("hidden");
});

$("settings-save").addEventListener("click", async () => {
  const status = $("settings-status");
  status.textContent = "保存中…";
  try {
    const payload = {};
    const map = { deepseek: "key-deepseek", openai: "key-openai", gemini: "key-gemini" };
    for (const [provider, inputId] of Object.entries(map)) {
      const value = $(inputId).value.trim();
      if (value) payload[provider] = value;
    }
    if (Object.keys(payload).length) {
      await api("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      Object.values(map).forEach((id) => ($(id).value = ""));
    }
    const newModel = $("new-model").value.trim();
    if (newModel) {
      await api("/api/models", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model_id: newModel }),
      });
      $("new-model").value = "";
    }
    await loadConfig();
    status.textContent = "已保存 ✓";
  } catch (err) {
    status.textContent = `保存失败：${err.message}`;
  }
});

/* ------------------------------------------------------------------ utils */

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

function formatDuration(seconds) {
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  if (m < 60) return `${m}m${String(s).padStart(2, "0")}s`;
  return `${Math.floor(m / 60)}h${String(m % 60).padStart(2, "0")}m`;
}

/* ------------------------------------------------------------------- boot */

(async function boot() {
  try {
    await loadConfig();
    updateTopicHint();
    await loadJobs();
    if (state.jobs.length) await selectJob(state.jobs[0].id);
    schedulePolling();
  } catch (err) {
    console.error(err);
    alert(`初始化失败：${err.message}`);
  }
})();
