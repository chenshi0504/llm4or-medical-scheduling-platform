const $ = (id) => document.getElementById(id);

const PERCENT_KEYS = new Set([
  "assignment_rate", "mandatory_coverage", "ratio", "improvement", "plateau_ratio", "cv",
]);

const LABELS = {
  objective: "目标值", assignment_rate: "安排率", mandatory_coverage: "强制覆盖率",
  violations: "硬违反", violations_by_code: "违反类型", runtime_s: "求解耗时（秒）",
  budget_s: "时间预算（秒）", ratio: "占预算", iterations: "迭代数",
  iterations_per_s: "每秒迭代", initial: "初始目标", final: "最终目标",
  improvement: "相对初始改善", t90: "达到 90% 的轮次", plateau_ratio: "平台期占比",
  points: "轨迹点数", degenerate: "轨迹持平", samples: "样本数", mean: "均值",
  sd: "标准差", cv: "离散系数", min: "最小值", max: "最大值",
};

const ORDER = [
  "objective", "assignment_rate", "mandatory_coverage", "violations",
  "runtime_s", "budget_s", "ratio", "iterations", "iterations_per_s",
  "improvement", "t90", "plateau_ratio", "points", "degenerate",
  "samples", "mean", "sd", "cv", "min", "max",
];

const MODE_LABEL = {
  status: "状态", thinking: "思考", assistant: "判断", tool: "工具", json: "程序",
  error: "异常", final: "结论", review: "复盘",
};
const EFFORT_LABEL = { low: "低", medium: "中", high: "高", xhigh: "超高", max: "最高" };

let selectedId = null;
let pendingParent = null;
let pollTimer = null;
let playToken = 0;
let playedFor = null;
let livePoints = [];
let currentTask = null;
let instanceMap = {};
let activeScenario = null;
let scenarioMap = {};
let modelSel = null;
let modelsInfo = {};

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "href") node.href = value;
    else if (key.startsWith("data-") || key.startsWith("aria-")) node.setAttribute(key, value);
    else node[key] = value;
  }
  for (const child of children) node.append(child);
  return node;
}

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function num(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value !== "number") return String(value);
  const fixed = Math.abs(value) >= 10 ? 1 : 3;
  return value.toLocaleString("zh-CN", { maximumFractionDigits: fixed });
}

function pct(value, signed = false) {
  if (value === null || value === undefined) return "—";
  return `${signed && value > 0 ? "+" : ""}${(value * 100).toFixed(1)}%`;
}

function format(key, value) {
  if (key === "improvement") return pct(value, true);
  if (PERCENT_KEYS.has(key)) return pct(value);
  return num(value);
}

function when(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

async function getJSON(url, options) {
  const response = await fetch(url, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `请求失败（${response.status}）`);
  return payload;
}

/* ---------------- 左侧：表单与记录 ---------------- */

async function loadHealth() {
  try {
    const data = await getJSON("/api/health");
    $("health").dataset.state = data.solver?.ok ? "ok" : "error";
    $("health").textContent = data.solver?.ok ? "求解器就绪" : "求解器不可用";
  } catch {
    $("health").dataset.state = "error";
    $("health").textContent = "无法连接服务";
  }
}

function instanceLabel(item) {
  return item.instance_id;
}

function instanceDetail(item) {
  if (!item) return "";
  if (item.scenario === "hhcrsp") {
    return `${item.patients ?? "?"} 患者 · ${item.clinicians ?? "?"} 护理员 · `
      + `${item.required_visits ?? item.visits ?? "?"} 访视 · ${item.days ?? "?"} 天`;
  }
  return `${item.patients ?? "?"} 名患者 · ${item.days ?? "?"} 天 · `
    + `${item.hospitals ?? "?"} 家医院 × ${item.ors_per_hospital ?? "?"} 个手术室`;
}

async function loadModels() {
  const data = await getJSON("/api/models");
  const seg = $("model-seg");
  seg.replaceChildren();
  modelsInfo = {};
  for (const item of data.models) {
    modelsInfo[item.id] = item;
    const button = el("button", { type: "button", class: "seg-btn", "data-model": item.id, title: item.name }, [
      el("b", { text: item.label }),
    ]);
    button.addEventListener("click", () => selectModel(item.id));
    seg.append(button);
  }
  const saved = localStorage.getItem("orbench.model");
  selectModel(modelsInfo[saved] ? saved : data.default);
  $("effort").addEventListener("change", () => {
    if (modelSel) localStorage.setItem(`orbench.effort.${modelSel}`, $("effort").value);
  });
}

function selectModel(id) {
  modelSel = id;
  document.querySelectorAll(".seg-btn").forEach((button) => {
    button.classList.toggle("on", button.dataset.model === id);
    button.setAttribute("aria-pressed", String(button.dataset.model === id));
  });
  localStorage.setItem("orbench.model", id);
  const info = modelsInfo[id] || { efforts: ["high"], default_effort: "high" };
  const select = $("effort");
  const saved = localStorage.getItem(`orbench.effort.${id}`);
  select.replaceChildren();
  for (const level of info.efforts) {
    select.append(el("option", { value: level, text: EFFORT_LABEL[level] || level }));
  }
  select.value = info.efforts.includes(saved) ? saved : info.default_effort;
}

async function loadInstances() {
  const items = await getJSON("/api/instances");
  const select = $("instance");
  select.replaceChildren();
  instanceMap = {};
  const groups = {};
  for (const item of items) {
    instanceMap[item.instance_id] = item;
    if (activeScenario && item.scenario !== activeScenario) continue;
    (groups[item.dataset] = groups[item.dataset] || []).push(item);
  }
  for (const [name, rows] of Object.entries(groups)) {
    const group = document.createElement("optgroup");
    group.label = `${name}（${rows.length}）`;
    for (const item of rows) {
      group.append(el("option", { value: item.instance_id, text: instanceLabel(item) }));
    }
    select.append(group);
  }
  updateInstanceHint();
}

function updateInstanceHint() {
  const item = instanceMap[$("instance").value];
  $("instance-hint").textContent = instanceDetail(item);
}

async function loadScenarios() {
  const items = await getJSON("/api/scenarios");
  const box = $("scenes");
  box.replaceChildren();
  scenarioMap = {};
  for (const item of items) {
    scenarioMap[item.id] = item;
    const group = document.querySelector(`[data-run="${CSS.escape(item.id)}"]`)?.closest("tbody");
    const problemType = group?.querySelector(".problem-group b")?.textContent;
    const card = el("button", { type: "button", class: "scene-card", "data-id": item.id, "aria-pressed": "false" }, [
      el("span", { class: "scene-name" }, [
        el("b", { text: problemType || item.name }),
        el("span", { class: "scene-count", text: `${item.instances} 个实例` }),
      ]),
      el("span", { class: "scene-meta", text: `${item.name} · ${item.constraints}` }),
      el("span", { class: "scene-datasets", text: item.datasets.join(" / ") }),
    ]);
    card.addEventListener("click", () => selectScenario(item.id));
    box.append(card);
  }
  const saved = localStorage.getItem("orbench.scenario");
  if (saved && scenarioMap[saved]) selectScenario(saved, { quiet: true });
}

let usageVersion = 0;
const decimal = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 });
const measured = (value) => Number.isFinite(value) && value >= 0;
function sumMeasured(values) {
  return values.length && values.every(measured) ? values.reduce((a, b) => a + b, 0) : null;
}

function phaseFigures(name, data) {
  const u = data.usage || {};
  const input = u.prompt_tokens, output = u.completion_tokens;
  const legacy = !("requests" in u);
  // Old builds saved latency for each concurrent stage. Match token and time scopes.
  let seconds = u.request_seconds;
  if (legacy) seconds = data.stages
    ? sumMeasured(data.stages.filter(s => s.model).map(s => s.latency_s))
    : data.latency_s;
  const hit = u.prompt_cache_hit_tokens;
  const total = sumMeasured([input, output]);
  return { name, input, output, seconds, hit, total: legacy && total === 0 ? null : total, legacy };
}

function usageRate(row) {
  return measured(row.output) && row.seconds > 0 ? row.output / row.seconds : null;
}
function hitRate(row) {
  return measured(row.hit) && row.input > 0 && row.hit <= row.input ? 100 * row.hit / row.input : null;
}
function showUsage(rows, body) {
  const known = rows.filter(row => measured(row.total));
  if (!known.length) {
    body.replaceChildren(el("p", { class: "usage-note", text: "尚无模型用量记录" }));
    return;
  }
  const total = Object.fromEntries(["total", "input", "output", "hit", "seconds"].map(key =>
    [key, sumMeasured(known.map(row => row[key]))]));
  const metrics = el("dl", { class: "usage-metrics" });
  for (const [label, value, unit] of [
    ["已记录用量", total.total, "tokens"], ["平均输出速度", usageRate(total), "tokens/s"],
    ["输入缓存命中率", hitRate(total), "%"],
  ]) {
    const dd = el("dd", { class: measured(value) ? "" : "missing", text: measured(value) ? decimal.format(value) : "未记录" });
    if (measured(value)) dd.append(el("span", { class: "unit", text: unit }));
    metrics.append(el("div", {}, [el("dt", { text: label }), dd]));
  }
  const table = el("table", { class: "usage-table", "aria-label": "各阶段模型用量" });
  const header = table.createTHead().insertRow();
  for (const text of ["阶段", "输入 tokens", "输出 tokens", "tokens/s", "命中率"]) header.append(el("th", { scope: "col", text }));
  const tbody = table.createTBody();
  for (const row of rows) {
    const tr = tbody.insertRow();
    tr.append(el("th", { scope: "row", text: row.name }));
    for (const [i, value] of [row.input, row.output, usageRate(row), hitRate(row)].entries()) {
      tr.insertCell().textContent = measured(row.total) && measured(value) ? decimal.format(value) + (i === 3 ? "%" : "") : "未记录";
    }
  }
  const note = known.some(row => row.legacy)
    ? "历史记录按已保存的用量汇总，速度按当时记录的耗时折算。"
    : "速度按输出 tokens 和累计请求耗时计算。";
  body.replaceChildren(metrics, el("div", { class: "usage-table-wrap" }, [table]), el("p", { class: "usage-note", text: note }));
}

async function loadUsage(task, home = false) {
  const version = ++usageVersion;
  const panel = $("model-usage"), body = $("usage-body");
  panel.hidden = false;
  const label = modelsInfo[task?.model]?.label || task?.model || "";
  $("usage-scope").textContent = task ? `${home ? "最近模型任务" : "任务"} #${task.id} ${label}` : "";
  if (!task) { showUsage([], body); return; }
  if (body.dataset.task !== String(task.id)) {
    body.dataset.task = task.id;
    body.replaceChildren(el("p", { class: "usage-note", text: "读取用量…" }));
  }
  try {
    const prefix = `/api/tasks/${task.id}`;
    const response = await fetch(`${prefix}/model-usage`);
    let data;
    if (response.status === 404) {
      // Older running services already expose persisted usage through these two routes.
      const [decision, build] = await Promise.all([getJSON(`${prefix}/decision`), getJSON(`${prefix}/pipeline`)]);
      data = { decision: decision.steps?.length ? decision : null, build,
        review: decision.steps?.some(step => step.mode === "review") ? {} : null };
    } else {
      if (!response.ok) throw new Error(`用量读取失败（${response.status}）`);
      data = await response.json();
    }
    if (version !== usageVersion) return;
    const rows = [];
    if (data.decision) rows.push(phaseFigures("决策", data.decision));
    if (data.build?.usage) rows.push(phaseFigures("建模", data.build));
    if (data.review) rows.push(phaseFigures("复盘", data.review));
    showUsage(rows, body);
  } catch (error) {
    if (version === usageVersion) body.replaceChildren(el("p", { class: "hint error", text: error.message }));
  }
}

function selectScenario(id, { quiet = false } = {}) {
  activeScenario = id;
  const item = scenarioMap[id] || {};
  document.querySelectorAll(".scene-card").forEach((card) => {
    card.classList.toggle("on", card.dataset.id === id);
    card.setAttribute("aria-pressed", String(card.dataset.id === id));
  });
  $("scene-form").hidden = false;
  $("scene-title").textContent = "任务参数";
  $("scene-hint").textContent = item.evidence || "";
  $("scene-hint").hidden = !item.evidence;
  localStorage.setItem("orbench.scenario", id);
  return loadInstances().then(() => {
    if (!quiet) {
      $("form-hint").textContent = "";
      $("form-hint").classList.remove("error");
    }
  }).catch((error) => {
    $("form-hint").classList.add("error");
    $("form-hint").textContent = error.message;
  });
}

async function loadTasks() {
  const items = await getJSON("/api/tasks");
  if (!selectedId && !new URLSearchParams(location.search).has("task"))
    loadUsage(items.find(task => task.use_deepseek), true);
  $("task-count").textContent = items.length;
  const list = $("task-list");
  list.replaceChildren();
  if (!items.length) {
    list.append(el("li", { class: "t-empty", text: "还没有记录。" }));
    return;
  }
  for (const item of items) {
    const running = ["preparing", "running"].includes(item.status);
    const verdict = running ? "running" : (item.verdict || "blocked");
    const label = item.status === "awaiting_start" ? "待启动" : running ? "运行中" : ({ pass: "通过", review: "待复核", blocked: "不通过" }[verdict] || "未完成");
    const link = el("a", { href: `/?task=${item.id}`, class: "task-link" }, [
      el("div", { class: "t-main" }, [
        el("span", { class: "t-instance" }, [el("span", { class: "run-index", text: `#${String(item.id).padStart(3, "0")}` }), document.createTextNode(item.instance_id)]),
        el("span", { class: `tag ${item.status === "awaiting_start" ? "running" : verdict}`, text: label }),
      ]),
      el("div", {
        class: "t-sub",
        text: `${scenarioMap[item.scenario]?.name || ""} · ` + (running
          ? `预算 ${item.wall_clock_s} 秒 · 种子 ${item.seed}`
          : `${item.wall_clock_s} 秒 · 种子 ${item.seed} · ${item.objective == null ? "无目标值" : "目标值 " + num(item.objective)}`)
          + (item.use_deepseek && item.model ? ` · ${modelsInfo[item.model]?.label || item.model}` : ""),
      }),
    ]);
    list.append(el("li", { "data-id": item.id }, [link]));
  }
}

/* ---------------- 提交与轮询 ---------------- */

async function submit(event) {
  event.preventDefault();
  const button = $("run-btn");
  const hint = $("form-hint");
  hint.classList.remove("error");
  button.disabled = true;
  hint.textContent = "正在创建任务…";
  try {
    const task = await getJSON("/api/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        instance_id: $("instance").value,
        seconds: Number($("seconds").value),
        seed: Number($("seed").value),
        use_deepseek: true,
        build_pipeline: $("build-pipeline").checked,
        model: modelSel,
        effort: $("effort").value,
        tool_mode: "ptc",
        parent_task_id: pendingParent,
      }),
    });
    pendingParent = null;
    selectedId = task.id;
    playedFor = null;
    livePoints = [];
    await loadTasks();
    showTask(task, { fresh: true });
    setView("ptc");
    startPolling(task.id);
    hint.textContent = `正在准备任务 #${task.id} 的决策`;
  } catch (error) {
    hint.classList.add("error");
    hint.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

function startPolling(id) {
  stopPolling();
  pollTimer = setInterval(() => poll(id), 700);
  poll(id);
}

function stopPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
}

async function poll(id) {
  if (selectedId !== id) return stopPolling();
  try {
    const data = await getJSON(`/api/tasks/${id}/progress`);
    if (data.decision_status === "ready" && playedFor !== id) renderDecision(id, false);
    if (["preparing", "running"].includes(data.status)) {
      updateRunning(data);
    } else {
      stopPolling();
      const task = await getJSON(`/api/tasks/${id}`);
      showTask(task, { fresh: false });
      await loadTasks();
      if (task.status === "succeeded" && task.use_deepseek && ["ready", "reviewing"].includes(task.decision_status)) {
        watchReview(id);
      }
    }
  } catch {
    stopPolling();
  }
}

/* ---------------- 决策过程 ---------------- */

async function renderDecision(id, instant) {
  playedFor = id;
  const panel = $("decision");
  try {
    const data = await getJSON(`/api/tasks/${id}/decision`);
    if (data.status === "skipped" || !data.steps?.length) {
      panel.hidden = true;
      return;
    }
    panel.hidden = false;
    const meta = $("decision-meta");
    meta.replaceChildren();
    if (data.model) {
      const label = modelsInfo[data.model]?.label || data.model;
      const effort = data.effort ? ` · ${EFFORT_LABEL[data.effort] || data.effort}` : "";
      meta.append(el("span", { class: "chip", text: `${label}${effort}` }));
    }
    if (data.rounds) meta.append(el("span", { class: "chip", text: `${data.rounds} 轮工具` }));
    if (currentTask?.id === id && !instant) loadUsage(currentTask);
    if (data.latency_s) meta.append(el("span", { class: "chip", text: `${data.latency_s}s` }));
    if (data.fallback) meta.append(el("span", { class: "chip warn", text: "本地回退" }));
    meta.append(el("span", { class: "chip", text: { reviewed: "已复盘", reviewing: "复盘中", ready: "已校验" }[data.status] || "进行中" }));

    const token = ++playToken;
    const stream = $("decision-stream");
    stream.replaceChildren();
    $("decision-foot").replaceChildren();
    const main = data.steps.filter((step) => (step.seq ?? 0) < 1000);
    const extra = data.steps.filter((step) => (step.seq ?? 0) >= 1000);
    for (let i = 0; i < main.length; i += 1) {
      if (token !== playToken) return;
      if (!instant) await wait(i === 0 ? 220 : 640);
      if (token !== playToken) return;
      stream.append(stepNode(main[i]));
      if (!instant) stream.lastElementChild.scrollIntoView({ block: "nearest" });
    }
    if (currentTask?.report && currentTask.id === id) appendFinalStep(currentTask);
    for (const step of extra) {
      if (token !== playToken) return;
      if (!instant) await wait(420);
      stream.append(stepNode(step));
      if (!instant) stream.lastElementChild.scrollIntoView({ block: "nearest" });
    }
    if (!instant) await wait(240);
    if (token !== playToken) return;
    $("decision-foot").replaceChildren(
      el("button", { class: "link", type: "button", text: "重播", onclick: () => renderDecision(id, false) }),
    );
    if (instant && currentTask?.status === "succeeded") setDecisionCollapsed(true);
  } catch {
    panel.hidden = true;
  }
}

function setDecisionCollapsed(collapsed) {
  const panel = $("decision");
  panel.classList.toggle("collapsed", collapsed);
  const toggle = $("decision-toggle");
  toggle.hidden = false;
  toggle.textContent = collapsed ? "展开决策" : "收起决策";
}

function stepNode(step) {
  const row = el("li", { class: `step mode-${step.mode} ${step.origin === "llm" ? "llm-output" : ""}` });
  row.append(el("span", { class: "chip", text: MODE_LABEL[step.mode] || step.mode }));
  const body = el("div", { class: "step-body" });
  body.append(el("div", {
    class: step.mode === "tool" ? "step-title mono" : "step-title",
    text: step.title,
  }));
  if (step.mode === "json") {
    body.append(el("details", { class: "raw" }, [
      el("summary", { text: "查看提交内容" }),
      el("pre", { text: step.text }),
    ]));
  } else if (step.mode === "thinking") {
    body.append(el("details", { class: "raw" }, [
      el("summary", { text: "展开思考" }),
      el("p", { class: "step-text muted", text: step.text }),
    ]));
  } else if (step.mode === "tool") {
    const arrow = step.text.indexOf(" → ");
    const head = arrow > 0 ? step.text.slice(0, arrow) : step.text;
    const tail = arrow > 0 ? step.text.slice(arrow + 3) : "";
    body.append(el("p", { class: "step-text mono", text: head }));
    if (tail) {
      if (tail.length <= 160) {
        body.append(el("p", { class: "step-text", text: `→ ${tail}` }));
      } else {
        body.append(el("details", { class: "raw" }, [
          el("summary", { text: "返回结果" }),
          el("pre", { text: tail }),
        ]));
      }
    }
  } else if (step.mode === "review") {
    body.append(el("p", { class: "step-text review", text: step.text }));
  } else {
    body.append(el("p", { class: "step-text", text: step.text }));
  }
  row.append(body);
  return row;
}

/* ---------------- 运行中 ---------------- */

function updateRunning(data) {
  if (currentTask) renderSummary(currentTask, { running: true, iteration: data.progress?.iteration });
  $("result").hidden = true;
  const running = $("running");
  running.hidden = false;
  const p = data.progress || {};
  if (data.decision_status === "thinking") {
    $("run-text").textContent = "模型正在选择算子与参数…";
  } else if (data.status === "preparing") {
    $("run-text").textContent = "正在打开命令行窗口…";
  } else if (p.state === "running" || p.iteration) {
    $("run-text").textContent = `第 ${num(p.iteration)} 轮 · 最好 ${num(p.best_objective)} · ${num(p.elapsed_s)} 秒`;
  } else {
    $("run-text").textContent = "已启动，等待第一轮回报…";
  }
  const stats = $("run-stats");
  stats.replaceChildren();
  const pairs = p.iteration ? [
    ["当前最优", num(p.best_objective)],
    ["本轮尝试", num(p.current_objective)],
    ["算子", `${p.destroy_operator || "—"} → ${p.repair_operator || "—"}`],
    ["命令行", data.console ? "已打开，结束后保留" : "未打开"],
  ] : [];
  for (const [k, v] of pairs) {
    stats.append(el("dt", { text: k }), el("dd", { text: v }));
  }
  if (p.state === "running" && Number.isInteger(p.iteration) && Number.isFinite(p.best_objective)) {
    if (livePoints.at(-1)?.[0] === p.iteration) livePoints.pop();
    livePoints.push([p.iteration, p.best_objective]);
    if (livePoints.length > 400) livePoints.shift();
  }
  drawChart($("live-chart"), livePoints);
}

/* ---------------- 结果 ---------------- */

function showTask(task, { fresh }) {
  selectedId = task.id;
  currentTask = task;
  $("hero").hidden = true;
  $("back-to-tasks").hidden = false;
  loadUsage(task);
  loadProposal(task);
  refreshWorkspace(task.id);
  document.querySelector(".view-tabs").hidden = false;
  renderSummary(task, { running: ["preparing", "running"].includes(task.status) });
  if (["preparing", "running"].includes(task.status)) {
    $("result").hidden = true;
    $("running").hidden = false;
    if (task.decision_status === "ready") renderDecision(task.id, !fresh);
    return;
  }
  $("running").hidden = true;
  if (task.decision_status !== "skipped") renderDecision(task.id, !fresh);
  renderResult(task);
}

function renderResult(task) {
  currentTask = task;
  renderSummary(task, { running: false });
  const root = $("result");
  root.replaceChildren();
  root.hidden = false;
  $("running").hidden = true;
  const report = task.report;
  if (!report) return;

  const head = el("div", { class: "d-head" }, [
    el("div", {}, [
      el("div", { class: "d-title", text: `任务 #${task.id} · ${task.instance_id}` }),
      el("div", {
        class: "d-meta",
        text: `${task.wall_clock_s} 秒预算 · seed ${task.seed} · ${when(task.created_at)}`
          + (task.executed_iterations ? ` · 执行 ${num(task.executed_iterations)} 轮` : "")
          + (task.stopped_by === "time_limit" ? "（时间到）" : ""),
      }),
    ]),
  ]);
  if (task.parent_task_id) head.append(el("div", { class: "d-meta", text: `基于任务 #${task.parent_task_id}` }));

  const box = el("div", { class: `verdict ${report.verdict}` }, [
    el("div", { class: "v-label", text: report.verdict_label }),
    el("p", { text: report.summary }),
  ]);

  const grid = el("div", { class: "grid" });
  for (const dim of report.dimensions) {
    const dl = el("dl");
    for (const key of ORDER) {
      if (!(key in dim.evidence) || dim.evidence[key] === null) continue;
      if (!LABELS[key]) continue;
      dl.append(el("dt", { text: LABELS[key] }), el("dd", { text: format(key, dim.evidence[key]) }));
    }
    grid.append(el("div", { class: `dim ${dim.status}` }, [
      el("h3", {}, [
        el("span", { text: dim.dimension }),
        el("span", { class: `tag ${dim.status}`, text: dim.label }),
      ]),
      el("p", { text: dim.note }),
      dl,
    ]));
  }
  root.append(head, box, grid);

  if (report.next_actions?.length) {
    const next = el("div", { class: "next" }, [el("h2", { text: "下一步" })]);
    for (const action of report.next_actions) {
      const button = el("button", { class: "ghost", type: "button", text: action.label });
      button.addEventListener("click", () => applyAction(task, action));
      next.append(button);
    }
    next.append(el("p", { class: "hint", text: "按建议会新建一个任务，原记录保留。" }));
    root.append(next);
  }

  root.append(renderChart(task));
  root.append(renderFiles(task));
  appendFinalStep(task);
  if (currentView === "trace") loadTrajectory(task.id);
  if (currentView === "model") loadModel(task.id);
  if (currentView === "build") loadPipeline(task.id);
}

function renderSummary(task, { running = false, iteration = null } = {}) {
  const box = $("summary");
  if (!task) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  const iter = iteration ?? task.executed_iterations;
  const items = [
    ["场景", scenarioMap[task.scenario]?.name || "—"],
    ["数据集", task.dataset || instanceMap[task.instance_id]?.dataset || "—"],
    ["实例", task.instance_id],
    ["预算", `${task.wall_clock_s} 秒`],
    ["执行", iter ? `${num(iter)} 轮${task.runtime_s ? " · " + task.runtime_s.toFixed(1) + " 秒" : ""}` : "—"],
    ["工具调用", task.tool_mode === "ptc" ? "PTC / Code Mode" : "历史原生调用"],
    ["主求解器", task.solver || (task.scenario === "hhcrsp" ? "hhcrsp_lab / ALNS" : "alns_lab / ALNS")],
  ];
  const verdict = running ? "running" : (task.report?.verdict || "");
  if (task.pipeline?.state) {
    const pl = task.pipeline;
    const text = pl.state === "done" ? `${pl.passed}/${(pl.passed || 0) + (pl.failed || 0)}`
      : pl.state === "running" ? "生成中" : "未完成";
    items.push(["建模", text]);
  }
  if (task.use_deepseek && task.model) {
    const label = modelsInfo[task.model]?.label || task.model;
    items.splice(4, 0, ["模型", `${label}${task.effort ? " · " + (EFFORT_LABEL[task.effort] || task.effort) : ""}`]);
  }
  box.replaceChildren();
  for (const [key, value] of items) {
    const tone = key === "结论" ? ` ${verdict}` : "";
    box.append(el("span", { class: "s-item" }, [
      el("span", { class: "s-k", text: key }),
      el("span", { class: `s-v${tone}`, text: value }),
    ]));
  }
  box.prepend(el("span", { class: "run-index summary-index", text: `#${String(task.id).padStart(3, "0")}`, "aria-label": `任务 ${task.id}` }));
}

function appendFinalStep(task) {
  const stream = $("decision-stream");
  if (!$("decision").hidden && stream.children.length && !stream.querySelector(".mode-final")) {
    stream.append(stepNode({
      mode: "final",
      title: "求解结束",
      text: `${task.report.verdict_label}；${task.report.summary}`,
    }));
  }
}

function applyAction(task, action) {
  const patch = action.patch || {};
  if (patch.seed !== undefined) $("seed").value = patch.seed;
  if (patch.wall_clock_s) $("seconds").value = patch.wall_clock_s;
  $("instance").value = task.instance_id;
  pendingParent = task.id;
  const hint = $("form-hint");
  hint.classList.remove("error");
  hint.textContent = `基于任务 #${task.id}：${action.reason}`;
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function renderChart(task) {
  const wrap = el("div", { class: "chart" }, [el("h2", { text: "迭代优化曲线" })]);
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  wrap.append(svg);
  getJSON(`/api/tasks/${task.id}/trace`)
    .then(({ points }) => drawChart(svg, points || []))
    .catch(() => drawChart(svg, [], "轨迹读取失败"));
  return wrap;
}

function drawChart(svg, points, emptyText = "暂无迭代记录") {
  svg.replaceChildren();
  svg.setAttribute("viewBox", "0 0 760 220");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "迭代次数与历史最优目标值");
  function add(tag, attrs, text) {
    const node = document.createElementNS(svg.namespaceURI, tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    if (text !== undefined) node.textContent = text;
    svg.append(node);
    return node;
  }
  const samples = points.filter(p => Array.isArray(p) && Number.isInteger(p[0]) && Number.isFinite(p[1]));
  if (!samples.length) {
    add("text", {x: 380, y: 110, "text-anchor": "middle"}, emptyText);
    return;
  }
  const first = samples[0], last = samples.at(-1);
  const values = samples.map(p => p[1]);
  const low = Math.min(...values), high = Math.max(...values);
  const yTicks = low === high ? [low] : [low, ...[0.25, 0.5, 0.75].map(t => low + (high - low) * t), high];
  const precision = high === low ? 6 : Math.max(3, Math.min(15, 1 - Math.floor(Math.log10((high - low) / 4))));
  const fmt = new Intl.NumberFormat("zh-CN", {maximumFractionDigits: precision});
  const left = Math.max(72, ...yTicks.map(y => fmt.format(y).length * 9 + 16));
  const right = 725, top = 32, bottom = 170;
  const xPos = x => left + (right - left) * (x - first[0]) / Math.max(1, last[0] - first[0]);
  const yPos = y => low === high ? (top + bottom) / 2 : bottom - (bottom - top) * (y - low) / (high - low);
  add("text", {x: left, y: 16}, "历史最优目标值");
  add("text", {x: (left + right) / 2, y: 215, "text-anchor": "middle"}, "迭代次数");
  for (const value of yTicks) {
    const y = yPos(value);
    add("line", {x1: left, x2: right, y1: y, y2: y, class: "chart-grid"});
    add("text", {x: left - 9, y: y + 4, "text-anchor": "end"}, fmt.format(value));
  }
  add("path", {d: `M${left},${top}V${bottom}H${right}`, fill: "none", class: "chart-axis"});
  const xTicks = new Set(Array.from({length: 6}, (_, i) => first[0] + Math.round((last[0] - first[0]) * i / 5)));
  for (const iteration of xTicks) {
    const x = xPos(iteration);
    add("line", {x1: x, x2: x, y1: bottom, y2: bottom + 4, class: "chart-axis"});
    add("text", {x, y: bottom + 19, "text-anchor": "middle"}, iteration.toLocaleString("zh-CN"));
  }
  add("path", {d: samples.map(([x, y], i) => `${i ? "L" : "M"}${xPos(x)},${yPos(y)}`).join(" "), class: "chart-line"});
  add("circle", {cx: xPos(last[0]), cy: yPos(last[1]), r: 3, class: "chart-dot"});
  for (const [i, [iteration, value]] of samples.entries()) {
    const x = xPos(iteration);
    const start = i === 0 ? left : (xPos(samples[i - 1][0]) + x) / 2;
    const end = i === samples.length - 1 ? right : (x + xPos(samples[i + 1][0])) / 2;
    const hit = add("rect", {x: start, y: top, width: end - start, height: bottom - top, fill: "transparent"});
    const title = document.createElementNS(svg.namespaceURI, "title");
    title.textContent = `第 ${iteration} 轮 · 历史最优目标值 ${value}`;
    hit.append(title);
  }
}

function renderFiles(task) {
  const wrap = el("div", { class: "files" }, [el("h2", { text: "产物" })]);
  if (!task.artifacts?.length) {
    wrap.append(el("span", { text: "本次没有落盘产物。" }));
    return wrap;
  }
  for (const name of task.artifacts) {
    wrap.append(el("a", { href: `/api/tasks/${task.id}/artifacts/${name}`, text: name,
      class: task.artifact_sources?.[name] === "llm" ? "llm-output" : "" }));
  }
  if (task.run_id) wrap.append(el("div", { class: "cap", text: `run_id: ${task.run_id}` }));
  return wrap;
}


/* ---------------- 轨迹视图 ---------------- */

const LANES = [
  { id: "input", label: "输入", color: "var(--lane-input)" },
  { id: "model", label: "模型", color: "var(--lane-model)" },
  { id: "tool", label: "工具", color: "var(--lane-tool)" },
  { id: "check", label: "校验", color: "var(--lane-check)" },
];

const ROLE_CLASS = {
  system: "r-system", context: "r-context", user: "r-user", thinking: "r-thinking",
  assistant: "r-assistant", tool: "r-tool", error: "r-error",
};

let currentView = "chat";
let traceData = null;
let traceTaskId = null;
let traceMode = "time";
let traceQuery = "";

function setView(view) {
  currentView = view;
  document.querySelectorAll(".view-tab").forEach((tab) => {
    tab.classList.toggle("on", tab.dataset.view === view);
  });
  for (const name of ["chat", "trace", "model", "build", "ptc", "code"])
    $(`view-${name}`).hidden = view !== name;
  if (view === "trace" && selectedId) loadTrajectory(selectedId);
  if (view === "model" && selectedId) loadModel(selectedId);
  if (view === "build" && selectedId) loadPipeline(selectedId);
  if (["trace", "ptc", "code"].includes(view) && selectedId) refreshWorkspace(selectedId);
}

async function loadTrajectory(id) {
  if (!id) return;
  $("lanes").replaceChildren(el("div", { class: "trace-empty", text: "读取轨迹…" }));
  try {
    const data = await getJSON(`/api/tasks/${id}/trajectory`);
    traceData = data;
    traceTaskId = id;
    renderTrace();
    const audit = await getJSON(`/api/tasks/${id}/audit`);
    if (selectedId === id) applyAuditToTrace(audit);
  } catch (error) {
    $("lanes").replaceChildren(el("div", { class: "trace-empty", text: error.message }));
  }
}

function renderTrace() {
  if (!traceData) return;
  renderLanes();
  renderTraceBody();
}

function renderLanes() {
  const data = traceData;
  const total = Math.max(data.total_ms || 0, 1);
  const wrap = $("lanes");
  wrap.replaceChildren();
  const head = el("div", { class: "lane-row lane-head" }, [
    el("span", { class: "lane-name", text: "" }),
    el("span", { class: "lane-meta", text: `${(total / 1000).toFixed(1)} 秒` }),
  ]);
  wrap.append(head);
  for (const lane of LANES) {
    const track = el("div", { class: "lane-track" });
    for (const phase of data.phases.filter((p) => p.lane === lane.id && p.ms > 0)) {
      const left = (phase.start / total) * 100;
      const width = Math.max((phase.ms / total) * 100, 0.6);
      const bar = el("span", { class: "lane-bar", title: `${phase.label} ${phase.ms} ms` });
      bar.style.left = `${left}%`;
      bar.style.width = `${width}%`;
      bar.style.background = lane.color;
      track.append(bar);
    }
    wrap.append(el("div", { class: "lane-row" }, [
      el("span", { class: "lane-name", text: lane.label }),
      track,
    ]));
  }
}

function renderTraceBody() {
  const data = traceData;
  const body = $("trace-body");
  body.replaceChildren();
  if (!data.rounds?.length) {
    body.append(el("div", { class: "trace-empty", text: "没有轨迹记录。" }));
    return;
  }
  if (traceMode !== "call" && data.system) {
    body.append(el("div", { class: "trace-system" }, [
      el("span", { class: "role r-system", text: "系统" }),
      el("span", { class: "sys-tag", text: "初始系统提示词" }),
      el("p", { text: data.system }),
    ]));
  }
  for (const round of data.rounds) {
    const events = (round.events || []).filter((event) => {
      if (traceMode === "call" && event.role !== "tool" && !event.label.startsWith("tool.") && event.label !== "ptc.code") return false;
      if (!traceQuery) return true;
      return `${event.label} ${event.text}`.toLowerCase().includes(traceQuery);
    });
    if (!events.length) continue;
    body.append(el("div", { class: "round-head", text: round.title }));
    for (const event of events) {
      const row = el("div", { class: `trace-event ${ROLE_CLASS[event.role] || ""} ${event.origin === "llm" ? "llm-output" : ""}` }, [
        el("span", { class: `role ${ROLE_CLASS[event.role] || ""}`, text: event.label }),
        el("p", { text: event.text }),
      ]);
      body.append(row);
    }
  }
  if (!body.children.length) {
    body.append(el("div", { class: "trace-empty", text: "没有匹配的记录。" }));
  }
}

function bindTrace() {
  document.querySelectorAll(".view-tab").forEach((tab) => {
    tab.addEventListener("click", () => setView(tab.dataset.view));
  });
  document.querySelectorAll(".trace-mode").forEach((btn) => {
    btn.addEventListener("click", () => {
      traceMode = btn.dataset.mode;
      document.querySelectorAll(".trace-mode").forEach((b) => b.classList.toggle("on", b === btn));
      renderTraceBody();
    });
  });
  $("trace-search").addEventListener("input", (event) => {
    traceQuery = event.target.value.trim().toLowerCase();
    renderTraceBody();
  });
}


/* ---------------- 模型视图 ---------------- */

let modelData = null;

async function loadModel(id) {
  const body = $("model-body");
  body.replaceChildren(el("div", { class: "trace-empty", text: "读取模型…" }));
  try {
    modelData = await getJSON(`/api/tasks/${id}/model`);
    renderModel();
  } catch (error) {
    body.replaceChildren(el("div", { class: "trace-empty", text: error.message }));
  }
}

function renderModel() {
  const data = modelData;
  const pipe = $("pipeline");
  pipe.replaceChildren();
  data.pipeline.forEach((step, index) => {
    if (index) pipe.append(el("span", { class: "pipe-arrow", text: "→" }));
    pipe.append(el("span", { class: "pipe-step" }, [
      el("b", { text: step.stage }),
      el("span", { text: step.text }),
    ]));
  });

  const body = $("model-body");
  body.replaceChildren();

  body.append(configCard(data));

  const vars = el("section", { class: "model-card" }, [
    el("header", {}, [
      el("h3", { text: "参数与变量" }),
      el("span", { class: "muted", text: "来自实例快照与当前解" }),
    ]),
  ]);
  const table = el("div", { class: "var-table" });
  for (const item of data.variables) {
    table.append(el("div", { class: "var-row" }, [
      el("code", { text: item.symbol }),
      el("div", {}, [
        el("div", { class: "var-name", text: item.name }),
        el("div", { class: "var-note", text: item.note }),
      ]),
      el("div", { class: "var-value", text: item.value }),
    ]));
  }
  vars.append(table);
  body.append(vars);

  const cons = el("section", { class: "model-card" }, [
    el("header", {}, [
      el("h3", { text: "约束" }),
      el("span", { class: "muted", text: `${data.constraints[0]?.code || ""}–${data.constraints[data.constraints.length - 1]?.code || ""} 由独立 checker 重算` }),
    ]),
  ]);
  const clist = el("div", { class: "cons-list" });
  for (const item of data.constraints) {
    const ok = ["pass", "modeled", "modeled_in_objective", "allowance"].includes(item.status);
    const row = el("div", { class: `cons-row ${ok ? "ok" : "bad"}` }, [
      el("span", { class: "cons-code", text: item.code }),
      el("div", { class: "cons-main" }, [
        el("div", { class: "cons-head" }, [
          el("span", { class: "cons-name", text: item.name }),
          el("span", { class: "cons-status", text: item.violations ? `违反 ${item.violations}` : (item.status === "pass" ? "通过" : "已建模") }),
        ]),
      ]),
    ]);
    const math = el("div", { class: "cons-math" });
    math.innerHTML = item.math || "";
    row.children[1].append(math);
    clist.append(row);
  }
  cons.append(clist);
  body.append(cons);

  const obj = el("section", { class: "model-card" }, [
    el("header", {}, [
      el("h3", { text: "目标函数" }),
      el("span", { class: "muted", text: "O2（主目标）" }),
    ]),
  ]);
  const math = el("div", { class: "obj-math" });
  math.innerHTML = data.objective.math;
  const defs = el("div", { class: "obj-defs" });
  defs.innerHTML = data.objective.definitions || "";
  obj.append(math, defs);
  const comps = el("div", { class: "obj-comps" });
  for (const item of data.objective.components) {
    if (item.value === null || item.value === undefined) continue;
    comps.append(el("span", { class: "s-item" }, [
      el("span", { class: "s-k", text: item.name }),
      el("span", { class: "s-v", text: num(item.value) }),
    ]));
  }
  obj.append(comps);
  obj.append(el("p", { class: "cap", text: `目标值 ${num(data.objective.value)}；${data.checker_mode || ""}${data.official_checker === false ? "，非官方 checker" : ""}` }));
  body.append(obj);
  body.append(el("p", {
    class: "cap",
    text: `数据集 ${data.dataset || "—"} · 假设登记 ${(data.assumptions || []).length} 条 · 变量与约束来自实例快照与 checker 重算`,
  }));
}

function configCard(data) {
  const card = el("section", { class: "model-card" }, [
    el("header", {}, [
      el("h3", { text: "本次配置" }),
      el("span", { class: "muted", text: data.previous ? `对比任务 #${data.previous.task_id}` : "同实例暂无可对比记录" }),
    ]),
  ]);
  const changed = new Set((data.diff || []).map((item) => item.field));
  const cfg = data.config;
  const prev = data.previous;
  const rows = [
    ["数据集", cfg.dataset || "—", prev ? (cfg.dataset || "—") : ""],
    ["算子", cfg.operators, prev ? prev.operators : ""],
    ["破坏比例", cfg.fraction ?? "—", prev ? (prev.fraction ?? "—") : ""],
    ["接受准则", cfg.acceptance || "—", prev ? (prev.acceptance || "—") : ""],
    ["时间预算", `${cfg.budget} 秒`, prev ? `${prev.budget} 秒` : ""],
    ["种子", String(cfg.seed), prev ? String(prev.seed) : ""],
    ["决策来源", cfg.fallback ? `${cfg.decision}（回退）` : cfg.decision, ""],
  ];
  const grid = el("div", { class: "cfg-grid" });
  grid.append(el("div", { class: "cfg-head", text: "配置项" }));
  grid.append(el("div", { class: "cfg-head", text: "本次" }));
  grid.append(el("div", { class: "cfg-head", text: prev ? `#${prev.task_id}` : "" }));
  for (const [key, now, before] of rows) {
    const isChanged = changed.has(key) && before && now !== before;
    grid.append(el("div", { class: `cfg-k${isChanged ? " changed" : ""}`, text: key }));
    grid.append(el("div", { class: `cfg-v${isChanged ? " changed" : ""}`, text: now }));
    grid.append(el("div", { class: "cfg-v muted", text: before }));
  }
  card.append(grid);
  if (data.diff?.length) {
    card.append(el("p", { class: "cap", text: `变动：${data.diff.map((d) => `${d.field} ${d.from} → ${d.to}`).join("；")}` }));
  }
  return card;
}


/* ---------------- 建模流水线 ---------------- */

const BUILD_NODES = [
  { id: "card", label: "知识卡" },
  { id: "A", label: "结构化规格" },
  { id: "B", label: "数学模型" },
  { id: "C", label: "求解代码" },
  { id: "V", label: "一致性校验" },
  { id: "R", label: "求解执行" },
];

let buildTaskId = null;
let buildTimer = null;

async function loadPipeline(id) {
  buildTaskId = id;
  if (buildTimer) clearTimeout(buildTimer);
  buildTimer = null;
  try {
    const data = await getJSON(`/api/tasks/${id}/pipeline`);
    renderPipeline(data);
    if (data.state === "running") pollPipeline(id);
  } catch (error) {
    $("build-body").replaceChildren(el("div", { class: "trace-empty", text: error.message }));
  }
}

function pollPipeline(id) {
  if (buildTimer) clearTimeout(buildTimer);
  buildTimer = setTimeout(async () => {
    if (buildTaskId !== id) return;
    try {
      const data = await getJSON(`/api/tasks/${id}/pipeline`);
      renderPipeline(data);
      if (data.state === "running") pollPipeline(id);
    } catch {
      /* 忽略一次失败，下一次再试 */
    }
  }, 2500);
}

async function startPipeline(id) {
  try {
    await getJSON(`/api/tasks/${id}/pipeline`, { method: "POST" });
    await loadPipeline(id);
  } catch (error) {
    $("build-body").replaceChildren(el("div", { class: "trace-empty", text: error.message }));
  }
}

function renderPipeline(data) {
  const strip = $("build-strip");
  const body = $("build-body");
  const meta = $("build-meta");
  meta.replaceChildren();
  const states = { card: data.contents?.["knowledge.json"] ? "done" : "idle", A: "idle", B: "idle", C: "idle", V: "idle", R: "idle" };
  states.R = currentTask?.status === "succeeded" ? "done" : currentTask?.status === "running" ? "running" : "idle";
  for (const stage of data.stages || []) states[stage.id] = stage.state;
  if (data.state === "done") states.V = data.validation?.ok ? "done" : "failed";
  if (data.state === "running") {
    states.A = data.contents?.["spec.json"] ? "received" : "running";
    for (const [key, file] of [["B", "model.json"], ["C", "solver.py"]]) {
      states[key] = data.contents?.[file] ? "received" : "idle";
    }
  }
  if (data.state === "idle") states.card = "idle";

  strip.replaceChildren();
  BUILD_NODES.forEach((node, index) => {
    if (index) strip.append(el("span", { class: "build-arrow", text: "›" }));
    const state = states[node.id];
    strip.append(el("span", { class: `build-node ${state}`, title: {done:"已完成",failed:"未通过",skipped:"已跳过",idle:"等待",running:"进行中",received:"已收到产物"}[state] }, [
      el("i", { text: { V: "✓", card: "◇", R: "▶" }[node.id] || node.id }),
      el("b", { text: node.label }),
    ]));
  });

  if (data.model) meta.append(el("span", { class: "chip", text: `${modelsInfo[data.model]?.label || data.model}${data.effort ? " · " + (EFFORT_LABEL[data.effort] || data.effort) : ""}` }));
  if (Number.isFinite(data.elapsed_s)) meta.append(el("span", { class: "chip", text: `${data.elapsed_s.toFixed(1)} s` }));
  if (data.state === "done" && currentTask?.id === data.task_id) loadUsage(currentTask);
  $("build-run").hidden = data.state === "running";
  $("build-run").textContent = data.state === "done" || data.state === "failed" ? "重新生成" : "生成";

  body.replaceChildren();
  if (data.state === "idle") {
    body.append(
      el("p", { class: "empty", text: "此任务尚未生成模型和代码。" }),
    );
    return;
  }
  if (data.state === "running") {
    body.append(el("div", { class: "running" }, [
      el("div", { class: "run-line" }, [el("span", { class: "dot" }), el("span", { text: "正在生成：结构化 → 数学模型 → 代码" })]),
    ]));
    return;
  }
  if (data.state === "failed") {
    body.append(el("p", { class: "empty", text: data.error || "生成失败。" }));
    return;
  }

  const validation = data.validation || {};
  const skipped = data.stages.filter(stage => stage.state === "skipped").length;
  const banner = el("div", { class: `build-verdict ${validation.ok ? "ok" : "failed"}` }, [
    el("b", { text: validation.ok ? "全部校验通过" : `${validation.failed} 项未通过` }),
    el("span", { text: `${validation.passed} / ${(validation.passed || 0) + (validation.failed || 0)} 项检查通过${skipped ? ` · ${skipped} 个阶段已跳过` : ""}` }),
  ]);
  body.append(banner);

  const contents = data.contents || {};
  for (const stage of data.stages || []) {
    const artifact = { A: "spec.json", B: "model.json", C: "solver.py" }[stage.id];
    const card = el("section", { class: `stage ${stage.state}` }, [
      el("header", {}, [
        el("span", { class: "stage-id", text: stage.id }),
        el("b", { text: stage.name }),
        ...(stage.model ? [el("span", { class: "chip", text: `${modelsInfo[stage.model]?.label || stage.model}${Number.isFinite(stage.latency_s) ? " · " + stage.latency_s.toFixed(1) + " s" : ""}` })] : []),
        el("span", { class: `tag ${{done:"ok",failed:"blocked",skipped:"muted"}[stage.state] || ""}`, text: {done:"通过",failed:"未通过",skipped:"已跳过"}[stage.state] || stage.state }),
      ]),
      el("p", { class: "stage-sum", text: stage.summary || "" }),
    ]);
    if (stage.error) card.append(el("p", { class: stage.state === "skipped" ? "cap" : "stage-error", text: stage.error }));
    const list = el("ul", { class: "checks" });
    for (const check of stage.checks || []) {
      list.append(el("li", { class: check.ok ? "ok" : "bad" }, [
        el("i", { text: check.ok ? "✓" : "✗" }),
        el("b", { text: check.id }),
        el("span", { text: check.name }),
        el("em", { text: check.detail || "" }),
      ]));
    }
    card.append(list);
    const text = contents[artifact];
    if (text) {
      card.append(el("details", { class: `raw ${artifactOrigin(data, artifact) === "llm" ? "llm-output" : ""}`, open: stage.id === "C" }, [
        el("summary", { text: `查看 ${artifact}` }),
        el("pre", { text }),
      ]));
    }
    body.append(card);
  }

  if (contents["knowledge.json"]) {
    body.append(el("details", { class: "raw" }, [
      el("summary", { text: "查看知识卡 knowledge.json" }),
      el("pre", { text: contents["knowledge.json"] }),
    ]));
  }
  body.append(el("p", { class: "cap", text: (data.boundary || {}).note || "" }));
}

/* ---------------- 入口 ---------------- */

let workspaceTimer = null;
let workspaceLoading = false;
let selectedArtifact = "solver.py";

async function loadProposal(task) {
  $("proposal").hidden = false;
  try {
    const data = await getJSON(`/api/tasks/${task.id}/proposal`);
    if (currentTask?.id !== task.id) return;
    const proposal = data.proposal;
    $("proposal-reason").textContent = proposal?.reasoning || "";
    $("proposal-state").textContent = proposal
      ? ({ awaiting_start: "决策已生成，尚未启动求解。", running: "正在执行以下决策。", succeeded: "本次实验使用的决策。" }[data.status] || "本次实验的决策。")
      : task.status === "preparing" ? "正在生成决策，求解器尚未启动。" : "此历史任务没有保存启动前决策产物。";
    if (proposal?.historical) $("proposal-state").textContent = "历史实验实际使用的 ALNS 参数（来自运行清单）。";
    if (task.error) $("proposal-state").textContent += ` ${task.error}`;
    const stamp = $("proposal-origin");
    stamp.textContent = proposal?.origin === "llm" ? "LLM 产出" : proposal?.fallback ? "本地回退" : "本地规则";
    stamp.hidden = !proposal;
    stamp.className = `tag ${proposal?.origin === "llm" ? "llm-output" : ""}`;
    $("proposal-values").replaceChildren();
    $("proposal-json").hidden = !proposal;
    $("proposal-json").className = proposal?.origin === "llm" ? "llm-output" : "";
    if (proposal) {
      for (const [key, value] of [
        ["破坏算子", proposal.program.destroy_operators.join(", ")],
        ["修复算子", proposal.program.repair_operators.join(", ")],
        ["破坏比例", `${(proposal.program.destroy_fraction * 100).toFixed(0)}%`],
        ["接受策略", proposal.program.acceptance],
      ]) $("proposal-values").append(el("dt", { text: key }),
        el("dd", { class: proposal.origin === "llm" ? "llm-output" : "", text: value }));
      $("proposal-json").textContent = JSON.stringify(proposal.program, null, 2);
    }
    $("start-solver").hidden = data.status !== "awaiting_start";
    $("start-solver").disabled = false;
  } catch (error) { $("proposal-state").textContent = error.message; }
}

async function startSolver() {
  const button = $("start-solver");
  button.disabled = true;
  $("start-error").textContent = "";
  try {
    const task = await getJSON(`/api/tasks/${selectedId}/start`, { method: "POST" });
    showTask(task, { fresh: false });
    setView("chat");
    startPolling(task.id);
    loadTasks();
  } catch (error) {
    $("start-error").textContent = error.message;
    button.disabled = false;
  }
}

async function loadSolverRoles() {
  try {
    const roles = await getJSON("/api/solver-roles");
    $("solver-roles").replaceChildren(
      el("dt", { text: "当前主求解" }), el("dd", { text: roles.primary.name }),
      el("dt", { text: "精确参考" }), el("dd", { text: `Gurobi · ${roles.gurobi.installed ? "本服务可导入" : "本服务未安装"} · 未接入当前执行` }),
      el("dt", { text: "代码生成目标" }), el("dd", { text: "Gurobi / gurobipy" }),
    );
    $("solver-roles").append(el("dt", { text: "Gurobi 位置" }), el("dd", { text: roles.gurobi.reference }),
      el("dt", { text: "参考模型" }), el("dd", { text: roles.gurobi.note }));
  } catch (error) { $("solver-roles").append(el("dd", { text: error.message })); }
}

function auditText(event) {
  return JSON.stringify(event.payload, null, 2);
}
function applyAuditToTrace(audit) {
  if (!traceData || traceTaskId !== audit.task_id) return;
  if (!audit.events.length) return;
  const events = [...audit.events].sort((a, b) => a.time - b.time);
  const created = Date.parse(currentTask?.created_at) / 1000;
  const first = Math.min(events[0].time, Number.isFinite(created) ? created : events[0].time);
  const pending = new Map(), phases = [];
  for (const event of events) {
    if (event.kind === "model.request") pending.set(event.phase, event);
    if (["model.response", "model.error"].includes(event.kind) && pending.has(event.phase)) {
      const start = pending.get(event.phase);
      phases.push({ lane: "model", label: event.phase,
        start: Math.max(0, (start.time - first) * 1000), ms: Math.max(0, (event.time - start.time) * 1000) });
      pending.delete(event.phase);
    }
    if (event.kind === "tool.call") pending.set(`${event.phase}:${event.payload.name}`, event);
    if (event.kind === "tool.result") {
      const key = `${event.phase}:${event.payload.name}`;
      if (pending.has(key)) {
        const start = pending.get(key);
        phases.push({ lane: "tool", label: event.payload.name,
          start: Math.max(0, (start.time - first) * 1000), ms: Math.max(0, (event.time - start.time) * 1000) });
        pending.delete(key);
      }
    }
  }
  const solverStart = Date.parse(currentTask?.solver_started_at) / 1000;
  const runtime = currentTask?.runtime_s;
  if (Number.isFinite(solverStart) && Number.isFinite(runtime) && runtime > 0) {
    phases.push({ lane: "tool", label: currentTask.solver || "ALNS",
      start: Math.max(0, (solverStart - first) * 1000), ms: runtime * 1000 });
    const check = traceData.phases.find(phase => phase.lane === "check");
    if (check) phases.push({ ...check, start: (solverStart - first + runtime) * 1000 });
  }
  traceData.phases = phases;
  traceData.total_ms = Math.max(1, (events.at(-1).time - first) * 1000,
    ...phases.map(phase => phase.start + phase.ms));
  renderLanes();
  traceData.system = null;
  traceData.rounds = [{
    title: "完整调用记录",
    events: events.map(event => ({
      role: event.origin === "llm" ? "assistant" : event.origin === "tool" ? "tool" : "context",
      label: event.kind,
      text: `${event.ts} / ${event.phase}\n${auditText(event)}`,
      origin: event.origin,
    })),
  }, {
    title: "任务生命周期",
    events: audit.lifecycle.map(event => ({ role: "tool", label: event.kind, text: `${event.ts}\n${event.detail}` })),
  }];
  renderTraceBody();
}

function renderPtc(audit) {
  const ptc = currentTask?.tool_mode === "ptc";
  const codes = audit.events.filter(event => event.kind === "ptc.code");
  $("ptc-state").textContent = ptc ? `${codes.length} 次 run_code` : "历史任务：原生工具调用";
  const body = $("ptc-body");
  body.replaceChildren();
  if (!codes.length) {
    body.append(el("p", { class: "hint", text: ptc && currentTask.status === "preparing"
      ? "等待模型生成工具编排代码。" : "此任务没有 PTC 编排代码记录。" }));
  }
  for (const event of audit.events.filter(event => event.kind.startsWith("ptc.") || event.kind.startsWith("tool."))) {
    const llm = event.origin === "llm";
    const entry = el("section", { class: `audit-entry ${llm ? "llm-output" : ""}` }, [
      el("h3", { text: `${event.kind} / ${event.payload.name || "run_code"}` }),
      el("p", { class: "audit-time", text: event.ts }),
      el("pre", { text: event.kind === "ptc.code" ? event.payload.code : auditText(event) }),
    ]);
    body.append(entry);
  }
}

function artifactOrigin(data, name) {
  const saved = data.artifacts?.find(item => item.path === name)?.origin;
  if (saved) return saved;
  const stage = data.stages?.find(item => item.artifact === name);
  return ["spec.json", "model.json", "solver.py"].includes(name) && stage?.model
    && data.contents?.[name] && !data.contents[name].startsWith("# 模型没有返回代码") ? "llm" : "system";
}

function renderCode(data) {
  const nav = $("code-files"), body = $("code-body");
  nav.replaceChildren();
  const contents = data.contents || {};
  const files = ["solver.py", "spec.json", "model.json", "knowledge.json", "validation.json", "manifest.json"].filter(name => contents[name]);
  $("code-state").textContent = data.state === "running" ? "模型生成中" : data.state === "done" ? "代码未执行" : "尚无代码产物";
  if (!files.length) {
    body.replaceChildren(el("p", { class: "hint", text: data.state === "running"
      ? "等待模型返回代码。" : "此任务尚未生成代码。" }));
    return;
  }
  if (!files.includes(selectedArtifact)) selectedArtifact = files[0];
  for (const name of files) {
    nav.append(el("button", { type: "button",
      class: `file-tab ${artifactOrigin(data, name) === "llm" ? "llm-output" : ""} ${name === selectedArtifact ? "on" : ""}`,
      text: name, "aria-pressed": String(name === selectedArtifact),
      onclick: () => { selectedArtifact = name; renderCode(data); } }));
  }
  const origin = artifactOrigin(data, selectedArtifact);
  const stage = data.stages?.find(item => item.artifact === selectedArtifact);
  const target = contents["solver.py"]?.includes("gurobipy") ? "Gurobi" : contents["solver.py"]?.includes("cp_model") ? "CP-SAT（历史产物）" : "以代码为准";
  body.replaceChildren(
    el("div", { class: "artifact-head" }, [
      el("span", { class: origin === "llm" ? "llm-output" : "", text: origin === "llm" ? `LLM 产出 / ${stage?.model || data.model || currentTask.model}` : "平台产出" }),
      el("a", { href: `/api/tasks/${currentTask.id}/artifacts/pipeline/${selectedArtifact}`, text: "下载文件" }),
    ]),
    el("p", { class: "artifact-meta", text: `${selectedArtifact === "solver.py" ? `目标：${target} · 执行：未执行 · ` : ""}${stage ? `静态检查：${stage.state === "done" ? "通过" : "未通过"} · 模型：${stage.model || "未记录"}` : ""}` }),
    el("pre", { class: `artifact-code ${origin === "llm" ? "llm-output" : ""}`, text: contents[selectedArtifact] }),
  );
}

async function refreshWorkspace(id) {
  if (workspaceLoading || selectedId !== id) return;
  workspaceLoading = true;
  clearTimeout(workspaceTimer);
  let repeat = false;
  try {
    const [audit, build] = await Promise.all([getJSON(`/api/tasks/${id}/audit`), getJSON(`/api/tasks/${id}/pipeline`)]);
    if (selectedId !== id) return;
    renderPtc(audit);
    renderCode(build);
    applyAuditToTrace(audit);
    $("audit-links").replaceChildren(...["model_trace.jsonl", "trace.json", "timeline.json"].map(name =>
      el("a", { href: `/api/tasks/${id}/artifacts/${name}`, text: name })));
    if (!audit.complete_messages) $("audit-links").append(el("span", { text: "历史任务仅保留决策摘要，未保存完整请求响应。" }));
    repeat = ["preparing", "running"].includes(currentTask.status) || build.state === "running";
    if (currentView === "build") renderPipeline(build);
  } catch (error) { $("ptc-state").textContent = error.message; }
  finally {
    workspaceLoading = false;
    if (selectedId && selectedId !== id) {
      workspaceTimer = setTimeout(() => refreshWorkspace(selectedId), 0);
      return;
    }
    if (repeat) workspaceTimer = setTimeout(() => refreshWorkspace(id), 2000);
  }
}

async function watchReview(id, tries = 10) {
  if (selectedId !== id || tries <= 0) return;
  await wait(2000);
  if (selectedId !== id) return;
  try {
    const data = await getJSON(`/api/tasks/${id}/decision`);
    if (data.status === "reviewed") {
      renderDecision(id, true);
      if (currentTask?.id === id) loadUsage(currentTask);
      return;
    }
    if (["reviewing", "ready"].includes(data.status)) return watchReview(id, tries - 1);
  } catch {
    return;
  }
}

async function openTask(id) {
  const task = await getJSON(`/api/tasks/${id}`);
  if (task.scenario && scenarioMap[task.scenario] && task.scenario !== activeScenario) {
    await selectScenario(task.scenario, { quiet: true });
  }
  if (instanceMap[task.instance_id]) {
    $("instance").value = task.instance_id;
    updateInstanceHint();
  }
  playedFor = null;
  livePoints = [];
  const nav = new URL(window.location.href);
  nav.searchParams.set("task", id);
  history.replaceState(history.state, "", nav);
  showTask(task, { fresh: false });
  if (["preparing", "running"].includes(task.status)) startPolling(id);
}

async function init() {
  // Existing non-reloading Flask processes may still serve the previous shell.
  if (!$("back-to-tasks")) {
    const response = await fetch("/static/workbench.html", { cache: "no-cache" });
    if (!response.ok) throw new Error(`界面加载失败（${response.status}）`);
    const page = new DOMParser().parseFromString(await response.text(), "text/html");
    page.body.querySelectorAll("script").forEach((node) => node.remove());
    document.body.replaceChildren(...page.body.children);
  }
  $("build-run").addEventListener("click", () => {
    if (selectedId) startPipeline(selectedId);
  });
  $("decision-toggle").addEventListener("click", () => {
    setDecisionCollapsed(!$("decision").classList.contains("collapsed"));
  });
  $("start-solver").addEventListener("click", startSolver);
  const catalog = $("candidate-scenes");
  const workspace = $("detail");
  workspace.after(catalog);
  function syncCatalog() {
    const open = location.hash === "#candidate-scenes";
    const wasOpen = !catalog.hidden;
    workspace.hidden = open;
    catalog.hidden = !open;
    $("catalog-open").setAttribute("aria-expanded", String(open));
    if (open && !wasOpen) $("candidate-title").focus();
    if (!open && wasOpen) $("catalog-open").focus({ preventScroll: true });
  }
  $("catalog-open").addEventListener("click", event => {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    if (!catalog.hidden) return;
    history.pushState({ catalogFromWorkspace: true }, "", "#candidate-scenes");
    syncCatalog();
  });
  $("catalog-back").addEventListener("click", () => {
    if (history.state?.catalogFromWorkspace) history.back();
    else {
      history.replaceState(null, "", location.pathname + location.search);
      syncCatalog();
    }
  });
  window.addEventListener("hashchange", syncCatalog);
  syncCatalog();
  const typeFilter = $("problem-type");
  const groups = Array.from(document.querySelectorAll(".candidate-table tbody"));
  for (const group of groups) {
    const name = group.querySelector(".problem-group b").textContent;
    typeFilter.add(new Option(name, name));
  }
  typeFilter.addEventListener("change", () => {
    for (const group of groups) {
      group.hidden = !!typeFilter.value && group.querySelector(".problem-group b").textContent !== typeFilter.value;
    }
  });
  loadSolverRoles();
  await loadHealth();
  await loadModels();
  await loadScenarios();
  await loadTasks();
  bindTrace();
  $("task-form").addEventListener("submit", submit);
  $("instance").addEventListener("change", updateInstanceHint);
  const params = new URLSearchParams(location.search);
  const wanted = params.get("scenario");
  if (wanted && scenarioMap[wanted]) selectScenario(wanted, { quiet: true });
  const id = Number(params.get("task"));
  const view = params.get("view");
  if (id) openTask(id).then(() => {
    if (["trace", "model", "build", "ptc", "code"].includes(view)) setView(view);
  }).catch(() => {});
}

init().catch((error) => {
  $("form-hint").classList.add("error");
  $("form-hint").textContent = error.message;
});

