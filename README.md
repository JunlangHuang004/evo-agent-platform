# EvoMAC 多 Agent 协作后端

通用多 Agent 课程项目。现有三页界面连接真实 HKGAI / DeepSeek 模型服务，后端提供可配置团队、任务 DAG、并行调度、工具网关、Docker 沙箱、人工审批、Artifact 交接与独立记忆。

## Mid Test 三场景适配

团队选择器保留原默认团队，另提供软件开发、新闻编辑室、证券基金三组。每组使用测试材料规定的四个角色和原文身份提示词，工具权限由服务端单独定义。团队分别保存会话、运行、记忆和文件，不能访问其他团队的数据。角色资料在 `backend/teams.py`，不能用模型输出扩大工具权限。

每个场景在同一群组连续发送 Task 1、Task 2。规划与行动会收到本组前文；上一轮已注册成果以有来源哈希的不可变快照继承，仅原作者或此前已获授权的组员取得只读导入。后续任务需要修改时，应在自己的工作区生成新版并重新发布；不会复制任意工作区、私聊或凭证。默认团队的历史数据保持原位置，三个场景保存在 `.runtime/teams/<team-id>/`。

界面提供团队切换、动态私聊频道、成员消息、审批答复及历史运行导出。生成的 HTML 只作为文本预览或下载，不在平台页面中直接执行。测试材料要求的交互仪表盘和 HTML slides 属于任务产物；平台本身仍保持简单界面。

真实测试入口从用户提供的测试文件夹读取六段原始任务，并核对角色提示词，没有预写回答、预设 DAG 或工具替身：

```sh
python3 benchmarks/mid_test_live.py --live \
  --suite-root '/path/to/Mid_Test(1)' \
  --config-root . \
  --data-dir /path/to/new-test-state \
  --output /path/to/new-test-results \
  --port 8081
```

该命令会消耗课程 API 额度，默认每项任务最多 48 次调用、600 秒，单次请求超时 60 秒、失败有限重试一次。两个输出目录都必须全新。测试期间可在本机 8081 界面回复真实澄清和审批；程序不会代答或自动批准。若 180 秒仍无人回复，保存等待状态，同组下一任务标为未运行，继续记录其他场景；重新打开保存的数据后可人工恢复。

结果逐项保存原始任务、完整对话、模型与工具记录、执行耗时和产物。规划步骤不计作业务子任务，继承产物与新增产物分开统计。`done`、角色参与、Docker exit 0 仅是执行证据；结果保持 `qualityVerdict=unassessed`，需要独立核验产物后才能计算真实任务完成率。此入口不配置或自动授权 Git 远端推送，模拟投资任务也不会执行交易。

## CrewAI 参考范围（2026-11-15）

对照 CrewAI 官方仓库固定提交 `f4da03af1be0f61db71c8599a40c06dce98b9b48` 的 [任务执行与 Guardrail 源码](https://github.com/crewAIInc/crewAI/blob/f4da03af1be0f61db71c8599a40c06dce98b9b48/lib/crewai/src/crewai/task.py)、[TaskOutput 源码](https://github.com/crewAIInc/crewAI/blob/f4da03af1be0f61db71c8599a40c06dce98b9b48/lib/crewai/src/crewai/tasks/task_output.py) 和 [SQLite 状态保存源码](https://github.com/crewAIInc/crewAI/blob/f4da03af1be0f61db71c8599a40c06dce98b9b48/lib/crewai/src/crewai/flow/persistence/sqlite.py)，并参考 [Tasks 文档](https://docs.crewai.com/en/concepts/tasks)。CrewAI 开源核心采用 [MIT 许可证](https://github.com/crewAIInc/crewAI/blob/f4da03af1be0f61db71c8599a40c06dce98b9b48/LICENSE)。这里借鉴交付约束、结构化交接、有限返工和可恢复状态的设计，自行实现与现有工具权限和不可变成果兼容的轻量机制，没有安装 CrewAI 或复制其源码，也没有接入 CrewAI 云服务。

- 可选 `outputContract` 声明文本、JSON 必需键或本任务要实际发布的文件类型与数量。结构检查只证明格式及文件存在，不证明业务正确、事实可靠或独立测试通过；未声明时明确记为未配置。
- `final` 声称完成前执行结构检查，不合格时返回具体问题并限制修复次数。历史继承的成果、其他子任务的文件和纯文字声明不能充当本任务的新文件。
- 文件数量按不同发布路径统计，同一路径重复注册或保存多个版本不能充当多个文件；每份快照的所有者、任务、哈希及大小仍须逐一核对。
- 参考源码中的错误反馈机制，坏 JSON 保留原响应、解析行列、调用 ID 和响应哈希，反馈给原 Agent，最多两次修复机会。已执行工具不会因修复格式而重放，原有调用/轮数/时间预算仍生效。
- `backend/task_output.py` 将真实结果、JSON 对象、文件 ID、格式检查、独立复核和工具失败分开交接。未知副作用与已证实失败分别记录，缺证据保持未知；依赖上下文不重复整段正文，压缩时保留状态和证据引用。公开历史按当前记录生成该对象，返工或后续复核不会留下旧的缓存结论。
- 继续使用本地 SQLite 现场和逐动作记录。只有暂停/中断任务提供恢复；已失败运行保留原始证据，重新尝试建立新运行，不自动重放有副作用的动作。
- 规划与具体行动分设输出预算，调用记录保留请求上限、实际返回模型和结束原因。超时、响应截断、工具错误和交付缺口分别呈现；增加等待时间不代表模型稳定性已经改善。

角色提示仍使用测试原文，模型服务仍限于课程允许的 HKGAI / DeepSeek；上述结构性改动的回归测试与真实六任务结果分别记录。

### 本轮验证结果（2026-11-15）

138 项本地回归全部通过，覆盖交付约束、坏 JSON 修复、结构化交接、上下文与成果继承、四角色调度以及团队 HTTP 隔离。此类测试主要使用可控模型响应，证明相应程序行为，不代表真实模型完成了业务任务。

随后按测试包原文连续执行三组、共六项真实任务。为限制重复等待，本轮显式设置单请求 30 秒、零自动重试、规划输出 1200 tokens、行动输出 3000 tokens；这些测试参数不改变正常启动时的默认配置。六项均在首次规划请求读取响应时超时，每项约 31 秒，共 6 次模型调用，没有进入业务子任务，也没有网页/代码工具调用或产物。Task 2 均保留对应 Task 1 的群组上下文。没有可用于独立验收的交付物，真实任务成功率提升尚未得到证明；未收到 usage 的请求也不能据此视为零消耗。

另行诊断中，短请求可成功返回，完整规划请求在 120 秒等待下仍超时。当前证据尚不能区分模型服务与本机转发链路的问题；没有更换课程模型、绕过代理、采用精简身份提示词，或用脚本模型替代实际运行。下一步应在正常长请求可稳定返回后，再复跑六任务并独立核验交付物。

## 启动

需要 Python 3.10+、Git 和 Docker。Python 后端仅使用标准库，无 npm / pip 依赖。

```bash
cp .env.example .env
# 在 .env 中填写课程 API key；已有本地配置时不要重复覆盖。
docker pull python:3.11-alpine
python3 server.py
```

打开 <http://127.0.0.1:8080/#workspace>。按 Ctrl+C 停止。可用 `python3 server.py --port 8081` 更换端口。不要用 `python3 -m http.server` 代替后端。

Docker 不可用或镜像未准备时，界面会如实显示，代码执行会失败，不会退回主机运行。模型凭证仅保存在本机 `.env`，不进入前端、容器、代码仓库或源码包。

## 已实现的执行流程

1. 群聊的“执行任务”模式先由模型返回结构化任务计划，后端验证 ID、成员、依赖与循环。
2. 调度器依据能力、工具权限、负载与成本选择参与者；独立任务可并行，依赖完成后才启动后续任务。
3. Agent 通过 JSON 动作协议调用获准工具，也可委派子任务、等待结果、拒绝任务、请求澄清和向成员交接信息。
4. 文本文件显式注册为不可变 Artifact；编排器按任务依赖授权只读副本，记录授权事件。
5. 覆盖、patch、删除和 git push 在具体动作审批处停止；批准、拒绝、修改参数及过期结果均持久化。
6. 任务产生执行经历与候选反思。只有经人工或可信结果确认的策略才能参与下次检索。
7. 服务中断后保留检查点；点击恢复继续。不能确认的外部写入必须先人工核对，系统不会直接重放。

“普通聊天”模式只调用模型，不启动任务和工具；私聊默认使用该模式，也可以切换到单 Agent 执行任务。

## 四个异构角色

| Agent | 能力和工具差异 |
| --- | --- |
| Coordinator | 规划、委派、澄清、文件处理与汇总 |
| Research | 私有文件处理、真实公开搜索和网页抓取 |
| Executor | 私有文件、Python 执行与测试、修复、Git |
| Reviewer | 独立文件、Python 测试、审查及有限返工 |

工具名单、参数 Schema、权限、风险、超时和幂等属性由后端定义。编排动作与工具返回均有事件记录。未选择的角色不会收到本次任务。

## 隔离和交接

代码仅在独立 Docker 容器内运行：自己的 run workspace 可写、显式 imports 只读、网络关闭、根文件系统只读、删除 Linux capabilities、限制 CPU / 内存 / 进程 / 文件大小 / 输出 / 时间。不会把 Docker socket、主机项目或 API key 挂入容器。

文件工具拒绝路径穿越、隐藏配置、符号链接、硬链接和特殊文件。修改审批绑定原始 SHA256，审批后文件变化会拒绝执行。公开网页工具逐跳检查地址并固定解析后的公网 IP，拒绝内网、metadata 和凭证 URL。

Git 元数据和 hooks 与代码 workspace 分离。push 只能使用固定 HTTPS `GIT_REMOTE`、本 Agent/run 分支及当时 HEAD 的精确提交，不允许 force。私有远端可单独配置 `GIT_TOKEN`；它仅注入获准 push 的 Git 子进程，不向 Agent 或容器公开。授权 commit 不会同时授权 push。

## 记忆进化

- Episodic：真实任务、动作、结果和失败原因，只作为经历归档。
- Procedural / Semantic：有来源、时间、适用条件、置信度、证据和成功失败计数的策略或知识。
- Working：当前 Agent 的有界工作上下文，过长时压缩，完整动作仍在审计日志中。
- 候选策略默认不可检索；在“Agents 与记忆”中明确确认后才可采用。
- 私聊经验不会自动注入群聊；跨 owner 共享必须显式授权。已确认的新经验才能替代旧经验，历史保留。
- 后续真实结果反馈按任务去重；连续失败会降权并要求重新确认，长期不用或低置信经验可归档。

## 可靠性与范围

支持模型/幂等工具的有限退避、请求和 Token 预留预算、时间和递归深度限制、重复动作/循环依赖检测、暂停取消、检查点、外部副作用幂等审计。人工扩展预算只允许一次并绑定当前任务。已发出的请求可能仍在结束；取消不会撤销已发生的变更。每次执行与请求还有独立超时，整体预算在动作边界检查。

当前代码执行仅支持 Python；搜索服务可能限流；模型不保证每次都返回有效 JSON 或完成任务。候选记忆经人工认可不等于已证明提升性能。运行状态“完成”表示调度结束，独立验收另行记录。

本次范围不包含课程最终隐藏评测集或性能对照实验。评测页只展示真实运行，缺少独立 verifier 的记录为“未评估”，不生成样例成功率。

## 目录

```text
server.py                   # 启动入口
backend/
  config.py                 # 本机配置和凭证脱敏
  teams.py                  # 场景团队、原文身份与角色权限
  contracts.py              # 文本、JSON、文件交付的结构检查
  task_output.py            # 成果、检查与失败证据的结构化交接
  server.py                 # 同源 JSON API 与静态文件白名单
  engine.py                 # 调度、Agent runtime、HITL 和恢复
  runtime.py                # 结构化动作协议与 DAG 校验
  agents.py                 # 四角色能力、工具和成本
  provider.py               # HKGAI / DeepSeek HTTP 传输
  tools.py                  # 文件、网页、代码、Git 工具网关
  sandbox.py                # 受限 Docker 与安全文件操作
  artifacts.py              # 注册、快照、授权和只读副本
  memory.py                 # 记忆确认、检索、反馈和遗忘
  persistence.py            # SQLite 迁移、事件、检查点、幂等审计
js/                         # 三页前端与 API 客户端
tests/                      # 单元和回归检查
.env.example                # 无密钥配置模板
.runtime/                   # 本地数据库/工作区；不进入 Git
```

## 接口与检查

- `GET /api/health`：服务、模型与 Docker 能力。
- `GET /api/state`：频道、任务、工具、审批、Artifact 和记忆。
- `GET /api/runs/<run-id>`：历史运行的公开信息。
- `POST /api/action`：`send / participants / pause / resume / recover / cancel / reset / approval / memory_confirm / memory_share / refresh_capabilities`。

POST 必须使用 `Content-Type: application/json` 和 `X-EvoMAC-Client: 1`；服务只监听本机，拒绝异源请求。静态白名单不能下载 `.env`、数据库或后端源文件。公开部署前需要补充身份认证。

```bash
python3 -m unittest discover -s tests -v
```

测试默认使用脚本模型、临时数据库和工具替身，不消耗模型额度。Docker/搜索/真实模型联调应单独运行。真实服务不可用会明确失败，不以测试替身作为实际系统的回退。

## 早期验证记录（2026-10）

回归检查覆盖调度并行、依赖、恢复、审批、预算、记忆与工具权限。另已用脚本模型配合真实 Docker、真实文件和 Artifact 验证跨 Agent 交接与审批；此项不代表真实模型端到端验收。真实公开搜索与网页抓取、Docker 隔离和本地 Git commit 均已单独验证。

早期探测中，真实 HKGAI 聊天、短 JSON 动作生成和结构化规划曾成功返回；完整模型驱动的编码—测试—独立复核流程尚未通过。后续四角色联调在规划阶段出现 API 超时。DeepSeek 和平台内真实 Git push 尚未联调。模型接入可用与多轮任务稳定完成需分别验收，建议演示前先测试课程服务状态。

进程正常运行时会跟踪并清理自己的 Docker 容器；后端被强制终止时，仍需人工检查遗留容器。

## 四角色协作验收

任务模式选齐四个 Agent 时，计划必须覆盖 Research、Executor、Reviewer 和 Coordinator 的实际任务。Coordinator 的最终汇总必须依赖另外三个角色的结果；缺失角色的计划会要求模型修正。明确指定的角色由能力约束绑定，不能由其他角色冒名完成。

代码任务只有取得 Reviewer 自己的成功 code.test 调用证据，且对应目标当前修订和 Artifact SHA，才能通过独立验收。缺证据、借用 Executor 的测试或返工后未复验时，运行显示部分完成并列出验收缺口。纯文档任务不强制代码测试。

2026-10-29 的 61 项回归已通过（历史基线）。另已完成脚本模型驱动的四角色真实工具联调：Research 抓取 CPython 官方资料，Executor 在 Docker 中执行及测试，Reviewer 在独立 Docker 中复核，只读 Artifact 逐步交接，Coordinator 生成最终报告。该联调仍不等同于真实模型全流程验收。

## 论文启发的记忆检索优化（2026-10-29）

采用按字段加权的 BM25F 风格词项排序：适用触发条件、使用条件、标题和经验内容分别参与评分，保留置信度和时间因素。先过滤未验证、不可见及未授权共享的记忆，再计算语料统计。只有 owner、范围、类型及全部检索字段相同的策略才在召回结果中去重；大小写、路径和适用条件保持区分，原记录与证据不会合并或删除。重复记录也不会改变 IDF 或字段长度统计。

研究依据包括 [A-Mem（NeurIPS 2025）](https://proceedings.neurips.cc/paper_files/paper/2025/hash/19909c36f51abc4856b4560aff3d36d6-Abstract-Conference.html) 的结构化记忆组织，以及 [ACE（ICLR 2026）](https://proceedings.iclr.cc/paper_files/paper/2026/hash/8a94ff6f922d995d7d3f4ebf4143e442-Abstract-Conference.html) 对保留有效策略细节的重视。本实现是轻量工程改动，没有复现论文的模型、训练或记忆图，也不继承论文报告的收益。

在人工构建、独立审核相关性标签后冻结的 **55 条策略 / 24 个中英查询**上，对比 `94b7030`：

| 指标 | 基线 | 当前版本 |
| --- | ---: | ---: |
| Top-1 命中率 | 83.33% | 91.67% |
| MRR | 0.899306 | 0.940972 |
| Precision@3 | 0.722222 | 0.763889 |
| 前三条中的重复槽位 | 5 | 0 |

这只是小型检索回归集；不是标准学术基准，也不是端到端 Agent 成功率。单条查询仍可能退步，平均注入上下文字符数没有减少。v2 标签纠正了 v1 的 8 个漏标及 4 个过度相关标注，修改原因记录在 fixture 的 `labelReview`，两个版本均使用同一 v2 标签重新评测。固定分母的 P@3 理论上限为 0.944444，因为两题只有一个直接相关策略。

```sh
python3 benchmarks/memory_retrieval.py
python3 -m unittest discover -s tests -v
```

基准保留完整逐题排名、v2 fixture 哈希与基线结果。也可用 `--source /path/to/baseline-checkout --output /path/to/results.json` 对另一检出目录复测，不调用模型或网络。

按 [ToolScope（ACL 2026）](https://aclanthology.org/2026.acl-long.1573/) 和 [MCP-Zero（2025 预印本）](https://arxiv.org/abs/2506.01056) 的思路试验过按需动作/工具说明，但 9 个合成场景各重复两次的交替 A/B 中，有效动作由 12/18 降至 9/18，含发现轮次的耗时增加，**该生产改动已撤下**。`benchmarks/protocol_live.py` 保留通用对比工具，仅在显式 `--live` 时请求课程 API；超时同样计为失败，不执行模型返回的工具。

`benchmarks/four_agent_live.py` 是另一个需要显式 `--live` 的真实模型、网页和 Docker 联调入口，需指定新的独立 `--data-dir`、本机 `--config-root` 和 `--output`。它不会自动批准任何动作，也不会配置 Git。本次真实 HKGAI 联调仍在规划阶段超时；该限制尚未解决。
