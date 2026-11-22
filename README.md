# LLM4OR 医疗平台

本机运行的排程求解与评价界面。先生成实验决策，查看模型选择的 ALNS 算子与参数，再点「启动求解」。页面同步显示进度，结束后用独立 checker 复算。

## 当前执行契约

- 新实验统一使用 DeepSeek PTC / Code Mode，界面与接口均不提供其他模式。PTC 沿用本机 DSH 的 `run_code`、`await tools.name(args)`、返回值与日志交互方式；运行时使用 QuickJS WASM，仅暴露三个注册工具，并非直接加载 DSH 包。历史任务按实际模式标注。运行前需在本目录执行 `npm ci --ignore-scripts`。
- 任务状态为 `preparing → awaiting_start → running → succeeded/failed`。准备阶段不会启动 worker；`POST /api/tasks/<id>/start` 验证已展示决策的 SHA256，再启动求解。同一任务只允许启动一次。
- `decision.json` 保存实际选择的算子、比例、接受策略、来源与回退状态。PTC 工具编排代码、完整模型请求/响应、工具输入输出及错误保存在 `model_trace.jsonl`；历史任务未保留的原始轨迹不会补造。
- 「代码与产物」直接展示 DeepSeek 返回的完整源码及静态检查结果。新代码生成以 `gurobipy` 为目标；生成代码不会被当作当前求解器执行。历史 CP-SAT 代码仍按实际内容展示。
- 当前实际求解为 `alns_lab` / `hhcrsp_lab`。Gurobi 的角色是精确参考求解器，仓库已有 `apps/platform/reference_llm_experiment.py` 的简化模型参考实现，尚未接入本平台两类原生实例的执行链。`GET /api/solver-roles` 返回当前 Python 环境的安装状态，不将可导入等同于许可证可用。
- LLM 产物以金黄色显示；工具结果与平台验证保留各自来源。用量汇总从原始调用记录计算，包含重试与再次生成；缓存字段缺失时显示未记录。
- `GET /api/tasks/<id>/proposal` 为启动前决策；`GET /api/tasks/<id>/audit` 为完整调用轨迹；`GET /api/tasks/<id>/artifacts/pipeline/solver.py` 可下载模型代码。

两个场景共用同一套任务表、轨迹、产物与评价口径，区别只在实例、模型与求解内核。

本目录是 `docs/reports/PLATFORM_V2_DESIGN.md` 的第一个可运行切片：单任务闭环。批量矩阵、arm 注册表、MCP 入口尚未实现。

## 场景

进入平台先选场景，再选该场景下的实例。

| 场景 | 实例 | 内核 | 模型 | 约束 |
|---|---|---|---|---|
| 手术排程 | FrontierOR 6 个 + IHTC2024 30 个 | `alns_lab` | 指派 x、开机 y、到院 z、未安排 u | K1–K11 |
| 居家护理路径（HHCRSP） | 华西 4 个规模 | `hhcrsp_lab` | 指派 x、启用 y、路线顺序 π | H1–H8 |

场景定义写在 `scenarios.py`，包括实例根目录、默认程序、模型摘要与证据边界。新增场景只需在这里登记一条，并在 `worker.py` 里接一个 runner——任务、轨迹、模型页与评价都复用现有结构。

## 界面

固定深色界面，正文以微软雅黑显示。英文使用与产物文件名相同的 Cascadia Mono / Consolas，数字优先使用 SF Pro / Segoe UI 的等宽数字。正文 15px。

最近任务默认折叠。首页展示最近模型用量；任务页先展示实验决策，然后提供对话、轨迹、PTC、代码与产物、建模和模型视图。公式使用 Cambria Math。

版心 1440px，左栏 310–360px、栏间距 40px。输入框和按钮使用 10px 圆角，场景项 16px，代码区 12px，分段选择内外圆角为 8px / 12px；支持连续圆角的浏览器使用 squircle。页面分区与表格以分隔线组织。右栏先展示实验决策与用量，再展示各页签和运行结果。

没有打开任务时，右栏展示模型用量和折叠的最近任务。

左栏从上到下：场景卡（选中后展开表单）、实例下拉与实例摘要、预算与种子、模型与推理强度、最近任务（默认折叠，只显示条数）。

旧的明暗偏好不会覆盖当前固定深色样式。

## 启动

```bash
# 方式一
双击 start.bat

# 方式二
"C:\Users\Administrator\.workbuddy\binaries\python\envs\token-monitor\Scripts\python.exe" -X utf8 app.py
```

默认 `http://127.0.0.1:8037`。也可以在平台总览 `http://127.0.0.1:8020/` 的「OR实战」里点「LLM4OR 医疗平台」启动。端口、数据库路径可用环境变量 `PORT`、`ORBENCH_DB` 覆盖，或复制 `config.example.json` 为 `config.json`。

> 服务必须从桌面会话启动，`CREATE_NEW_CONSOLE` 拉起的命令行窗口才会出现在你的屏幕上。以服务方式后台运行时不保证可见。

## 一次任务包含什么

| 环节 | 说明 |
|---|---|
| 输入 | 场景、实例、时间预算、种子；DeepSeek 统一以 PTC 生成决策 |
| 决策 | 在线 DeepSeek 走三轮工具调用（实例画像 → 容量压力 → 程序校验）后返回受限程序；不通过则回传错误重试一次，仍不过才回退规则。两个场景各有自己的工具集、白名单与提示词 |
| 求解 | 新开命令行窗口跑 `worker.py`，按场景分派到 `alns_lab` 或 `hhcrsp_lab`，逐轮打印；窗口结束后保留，手动关闭 |
| 进度 | worker 每 0.5 秒写 `progress.json`，页面每 0.7 秒读一次，画 best-so-far |
| 校验 | 场景自己的独立 checker（K1–K11 / H1–H8），指标由 checker 重算 |
| 评价 | 解质量、运行时间、收敛性、稳定性；结论分通过 / 待复核 / 不通过 |
| 复盘 | 求解结束后再请一次模型读结果，给出观察、判断与下一步；失败则跳过，不编内容 |
| 记录 | `orbench.db` 三张业务表 + `decisions`；产物在 `results/orbench/runs/<run_id>/` |

## 数据集

| 数据集 | 来源 | 数量 | 说明 |
|---|---|---|---|
| FrontierOR `roshanaei2017` | HuggingFace `frontieror/FrontierOR` | 6 | 原生语义，固定 revision 与逐文件 SHA-256 |
| IHTC 2024 | `ihtc2024.github.io` 官方实例包 | 30 | **确定性简化**：忽略护士、床位、班次与释放日；目标改为 reward − 开机/加班成本 |
| HHCRSP 华西 | 华西医院案例的已注册算例（`table3-conversion-v1`） | 4 | **确定性前缀裁剪**：30/60/90/120 名患者，护理员 6/10/20/30；七天、60 分钟统一时间窗、按次访视 |

下拉框按数据集分组。IHTC 的转换脚本在 `scripts/import_ihtc2024.py`（`--zip data/instances/ihtc2024/_source.zip`），产物写 `data/instances/ihtc2024/`，同目录 `manifest.json` 记录来源哈希与语义损失。IHTC 结果只能标注为“IHTC2024 确定性简化”，不能当作 IHTP 官方成绩。

HHCRSP 的四个规模由 `scripts/import_hhcrsp_huaxi.py` 从注册算例 `huaxi_p120_k030_s11` 派生：取前 N 名患者，按技能覆盖与可用天数挑 K 名护理员，旅行矩阵按同一 `node_order` 子集切片。访视频次、时间窗、服务时长不重算——它们由源算例的转换规则一次性决定。`data/instances/hhcrsp_huaxi/manifest.json` 记录源文件 SHA-256、派生规则与四条语义损失。

## 模型

平台接的是 DeepSeek 的两个线上模型，与 `https://api.deepseek.com/models` 返回的两个条目一致：

| 模型 id | 显示 | 上下文 | 推理强度 | 适用 |
|---|---|---|---|---|
| `deepseek-flash` | Flash | 1,048,576 | 低 / 高 / 最高 | 响应快，适合反复试配置 |
| `deepseek-v4-pro` | Pro | 1,048,576 | 低 / 高 / 最高 | 推理更稳，适合难实例与复盘 |

表单里的 Flash / Pro 卡片与「推理强度」下拉即选即用，选择记在 `localStorage`，随任务写进 `tasks.model` / `tasks.effort`，再传给 `chat/completions` 的 `model` 与 `reasoning_effort`。决策面板顶部的徽标显示实际返回的模型、强度、工具轮次、token 数与决策耗时——不是前端自报，而是响应里的 `model` 与 `usage`。

两个模型都支持 OpenAI 风格的 `tools` 与 `tool_choice`，所以决策层可以真的调工具，而不是只做一次问答。

### 与 DSH 的对应

本机 DSH 是 Electron 打包，轨迹页在 `app.asar` 里，不是可 import 的插件；它的源码在 `D:\Application_downloads\deepseek harness\deepseek-harness-master`。这里不搬它的代码，只按它的交互契约重写：

| DSH 的做法 | 本平台的落法 |
|---|---|
| 模型选择 + 推理强度 | 表单里的 Flash / Pro 卡片与「推理强度」，参数名同为 `reasoning_effort` |
| 先取事实再动手 | 强制先调 `inspect_instance` / `estimate_pressure`，再提交程序 |
| 工具调用与结果回填 | 三轮 `tool_calls` 循环，工具结果以 `role: tool` 回填 |
| 工具轮次用尽要收口 | 关闭工具，要求只输出 JSON，避免空转 |
| 轨迹按轮次与角色呈现 | 轨迹页第 1/2/3 轮，角色色块：系统、上下文、用户、助手、工具 |
| 运行后复盘 | 求解结束再请一次模型读结果，单独成「复盘」步 |

差别在于：DSH 的决策是开放的，本平台的决策被白名单与独立 checker 夹住，任何越界都会退回本地规则并留痕。

## DeepSeek 决策的展示

决策过程按顺序出现在首页右栏，每步间隔约 0.6–0.8 秒，不一次性铺开。七种输出模式：

| 模式 | 内容 | 呈现 |
|---|---|---|
| 状态 | 读取实例 | 灰色行，一行事实 |
| 思考 | 模型的 `reasoning_content` | 默认折叠，点开才看 |
| 工具 | 工具名 + 入参 + 返回结果（`inspect_instance` / `estimate_pressure` / `validate_program`） | 等宽工具名；长结果折叠 |
| 判断 | 为什么选这组算子 | 正文段落 |
| 程序 | 受限 JSON | 折叠，点开才显示原文 |
| 异常 | 在线失败、校验不过、输出不可解析 | 红色标签 + 回退说明 |
| 复盘 | 运行结束后的观察、判断与下一步 | 紫色标签，接在结论之后 |

一次决策最多三轮工具调用：先取实例画像，再估容量压力，然后提交候选程序校验；校验不过会把错误回传给模型重试一次，仍不过才回退本地规则。工具轮次用完还没给 JSON 时，会关掉工具强制收口，避免空转。

### 对话 / 轨迹

右栏提供对话、轨迹、PTC、代码与产物、建模和模型六个页签。对话查看决策与结果，轨迹查看完整原始调用，PTC 查看实际编排代码及工具返回。

轨迹页有三段：

- 顶部三行泳道：输入 / 模型 / 工具 / 校验，按真实耗时比例铺色块，右上角是总时长；
- 「时长 / 轮次 / 调用」切换视图，右侧搜索框过滤文本；
- 下方按轮次列事件：系统提示、上下文、用户、助手、工具，角色色块与 DSH 一致。

数据来自 `model_trace.jsonl`（完整请求、响应与工具调用）、`timeline.json`（阶段时长）、`decisions`（决策步骤）与任务记录；旧任务缺少的原始记录不补造。

调用失败、超时或 JSON 不合规时，一律退回规则程序（`worst_due` + `regret_k`，0.15，sa），并在决策流里标明“本地回退”。

## 建模流水线

「建模」页签对应参考图（CPAM-LLM, Nature Communications 2026）里的 Online Inference Pipeline：不同模块各做一件事，每段的产物都落盘、可打开检查。

| 段 | 输入 | 产出 | 校验 |
|---|---|---|---|
| 知识卡 | 数据集 manifest + 实例画像 | `knowledge.json` | 只读，不生成 |
| A 自然语言结构化 | 事实 + 约束目录 | `spec.json`：参数 / 变量 / 约束 / 目标 | 四块齐全、约束编号全覆盖、关键数字与算例一致 |
| B 数学模型 | spec | `model.json`：每条约束的 MathML + 目标式 | 编号一致、公式是合法 MathML 且不过长 |
| C 求解代码 | spec | `solver.py`：Gurobi / gurobipy 模型代码 | 可解析、依赖白名单、有 `build_model` / `solve`、无危险调用、覆盖约束编号；历史 CP-SAT 产物保留 |
| 一致性校验 | 三段产物 | `validation.json` | 逐项列出，界面显示 13 项通过情况 |

打开「建模」页签就能看到 5+1 个节点（知识卡 → 结构化 → 数学模型 → 求解代码 → 一致性校验 → 求解执行），每段一张卡：状态、模型、耗时、每条检查的通过与否、以及可展开的原始产物。生成约 30–70 秒，后台跑，完成后自动刷新，可「重新生成」。

**代码段只做静态校验，不执行。** 真正求解仍是 `alns_lab` / `hhcrsp_lab`，结果由独立 checker 复算；这样「可检查」不等于把不可控代码塞进实验链路。生成失败会如实标红并保留原文，不伪造通过。

## 模型页签

「模型」把这次任务对应的运筹优化三要素摊开，不是示意图，全部来自实例快照与 checker 结果：

| 块 | 内容 | 来源 |
|---|---|---|
| 参数与变量 | 参数规模（患者/天/医院/手术室/医生）、x/y/z/u 四组决策变量及当前取值、容量与负荷率 | `instance_snapshot.json` + `metrics.components` |
| 约束 | K1–K11（手术排程）或 H1–H8（HHCRSP）逐条状态与 MathML 公式 | `checker_result.constraints` |
| 目标函数 | 目标公式、变量定义、目标值与分项 | `checker_result.objective_formulas` + `metrics.components` |

HHCRSP 的三要素：指派变量 `x[p,k,d]`、启用变量 `y[k,d]`、路线顺序 `π[k,d]`；约束为技能匹配、排班可用、访视频次、同日不重复、最小间隔、服务时间窗、日工时上限、路线闭合；目标为 `min Σ [f_k·y + c·旅行分钟]`。

顶部一行是这次运行的链路：结构化（实例与决策口径）→ 数学模型（变量·约束·目标）→ 求解（ALNS + 独立校验）。

公式用浏览器原生 MathML 排版，不依赖 CDN 或前端数学库；约束公式与约束编号一一对应，状态与违反数仍由 checker 重算。

顶部「本次配置」列出生效的算子、破坏比例、接受准则、时间预算、种子与决策来源；同实例存在上一条记录时并排对比，变动的行标蓝，并给出一行 `from → to` 摘要。

## 与 CPAM-LLM 的对照

参考：Shi 等，*Automated data to model transformation for constraint programming with AI*，Nature Communications 2026（华中科技大学）。它做的是「自然语言 → 经校验的约束规划模型 → 求解代码」，靠 FCA-RAG 知识库、文本/模型/代码三侧数据增强、LoRA 微调与求解器反馈闭环。

我们目前的定位不同，也刻意不同：

- **实验执行模型是冻结的**：L0 重建模型 + K1–K11 由 `alns_lab` 固定，LLM 决定算子与参数，另外生成可检查的模型与 Gurobi 代码产物。生成代码尚未接入实际执行。
- **校验是独立的一层**：checker 不采信 LLM 输出，三要素页签展示的是重算结果，不是模型自述。
- **知识库很薄**：每个数据集只有一份 manifest（来源哈希 + 语义损失），没有 CPAM-LLM 那种结构化概念关联。

可借鉴的三点，按可行性排序：

1. **把三要素做成机器可读的中间产物**（已做）：变量、约束、目标各自成表，后续可以让 LLM 引用具体约束编号，而不是复述自然语言。
2. **让校验反馈进入下一轮提议**：现在失败只回退，不解释；可以把 violation code 与冲突槽回传，形成「提议 → 校验 → 修正」的小闭环。
3. **补一层数据集知识卡**：把每个数据集的字段映射、丢掉的语义、可用算子写成结构化条目，在提示词里检索，而不是塞进固定系统提示。

已提供自动生成求解器代码与静态校验；原始代码和失败检查均可查看下载。当前实验执行仍使用已登记的求解内核。
## 时间预算与迭代

页面只暴露时间预算（5–300 秒），不填迭代数。求解器以 50 万轮为安全上限，按时间停止；轨迹按 20 Hz 采样，避免长预算把内存撑爆。收敛性维度基于采样点计算，平线会如实标注“轨迹持平”。

tiny 实例常见首解即最优，曲线是平的；要看收敛过程用 `large_instance_*`。

## 评价口径

不打 0–100 分。每个维度给状态（正常 / 需复核 / 不通过 / 样本不足）和证据数值。

| 维度 | 判断依据 |
|---|---|
| 解质量 | 手术排程：checker 是否通过、硬违反条数与类型、安排率、强制患者覆盖率；HHCRSP：访视落实数 / 应访视数、启用路线数 |
| 运行时间 | 按预算停止视为正常；未到预算却超过上限才判不通过 |
| 收敛性 | 相对初始解的改善幅度（HHCRSP 目标越小越好，方向已按此计算）；零改进标需复核；低于 2% 提示余量不足 |
| 稳定性 | 同实例历史样本；少于 3 个标样本不足，≥3 个按离散系数（≤5% 正常，≤15% 需复核）；少于 5 个不判不通过 |

阈值在 `evaluation.py` 顶部。改动阈值等于改口径，需同步改本表。

## 控制台窗口

每次运行弹出独立 CMD 窗口，逐轮打印迭代、当前最优、本轮尝试、算子与用时；结束后停在提示符，等你手动关闭。页面不依赖窗口是否可见，进度来自 `progress.json`。

若 20 秒没有任何进度回报，任务标为失败并写明“命令行窗口没有回报进度”，避免无限转圈。

## 接口

| 路由 | 用途 |
|---|---|
| `GET /api/models` | 可接入的两个模型与推理强度 |
| `GET /api/scenarios` | 场景清单与各自实例数 |
| `GET /api/instances` | 全部实例（含场景字段） |
| `POST /api/tasks` | 仅创建 PTC 任务并准备决策，完成后等待启动 |
| `POST /api/tasks/<id>/start` | 校验决策哈希后启动求解 |
| `GET /api/tasks/<id>/progress` | 状态、决策状态、逐轮进度 |
| `GET /api/tasks/<id>/decision` | 决策步骤与回退标记 |
| `GET /api/tasks/<id>` | 任务与四维评价 |
| `GET /api/tasks/<id>/trace` | 采样后的轨迹 |
| `GET /api/tasks/<id>/model` | 三要素（按场景返回 K1–K11 或 H1–H8） |
| `GET /api/tasks/<id>/pipeline` | 建模产物、校验结果与原始文件 |
| `POST /api/tasks/<id>/pipeline` | 重新生成建模产物 |
| `GET /api/tasks/<id>/artifacts/<name>` | 白名单产物下载 |

## 代码结构

| 文件 | 职责 |
|---|---|
| `app.py` | 路由、任务表、按场景分派、评价落盘、模型页数据 |
| `scenarios.py` | 场景注册表与实例扫描 |
| `decision.py` | 两个场景的提示词、工具集、白名单；带工具调用的决策循环与复盘 |
| `worker.py` | 命令行窗口进程，按 `--scenario` 选 runner |
| `runner.py` / `runner_hhcrsp.py` | 调用内核、独立复算、写产物 |
| `evaluation.py` | 四维评价（`evaluate` / `evaluate_hhcrsp`） |
| `pipeline.py` | 建模流水线：结构化 → 数学模型 → 代码 → 校验 |

HHCRSP 内核在仓库根目录 `hhcrsp_lab/`：`instance.py` 读取实例，`model.py` 走时间轴并校验 H1–H8，`alns.py` 破坏—修复，`metrics.py` 汇总。

## 边界

- checker 是 `reconstructed-independent`，不是 FrontierOR 官方实现；“通过”只表示按当前重建模型与 K1–K11 无违规。
- HHCRSP 是**名义模型**：服务时长与旅行时间都取实例登记的均值，双预算鲁棒参数只登记、不参与求解；因此不能标注为 DBR-ALNS 的鲁棒结果。
- 华西算例是注册实例的前缀裁剪，不是医院官方统计口径，也不是论文报告的成绩。
- 决策层会消耗 token：一次任务通常 3–5k input、约 1k output，Pro 与「最高」强度会更慢更贵；接口按次计费，平台不做预算拦截。
- 建模产物额外消耗 8–13k tokens；三段各自独立校验，代码段不执行，不能当作「已求解」。
- 不代表临床可用，不证明全局最优。
- 单机、无鉴权、同一时间只跑一个任务；不要暴露到公网。
- 稳定性只做同参数离散程度，不做跨算法比较。
