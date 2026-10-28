# EvoMAC 多 Agent 协作后端

通用多 Agent 课程项目。现有三页界面连接真实 HKGAI / DeepSeek 模型服务，后端提供任务 DAG、并行调度、工具网关、Docker 沙箱、人工审批、Artifact 交接与独立记忆。

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

## 本次验证记录

回归检查覆盖调度并行、依赖、恢复、审批、预算、记忆与工具权限。另已用脚本模型配合真实 Docker、真实文件和 Artifact 验证跨 Agent 交接与审批；此项不代表真实模型端到端验收。真实公开搜索与网页抓取、Docker 隔离和本地 Git commit 均已单独验证。

真实 HKGAI 聊天、短 JSON 动作生成和结构化规划已成功返回；完整模型驱动的编码—测试—独立复核流程仍因执行阶段 API 超时未通过。DeepSeek 和真实 Git push 尚未联调。模型接入可用与多轮任务稳定完成需分别验收，建议演示前先测试课程服务状态。

进程正常运行时会跟踪并清理自己的 Docker 容器；后端被强制终止时，仍需人工检查遗留容器。

## 四角色协作验收

任务模式选齐四个 Agent 时，计划必须覆盖 Research、Executor、Reviewer 和 Coordinator 的实际任务。Coordinator 的最终汇总必须依赖另外三个角色的结果；缺失角色的计划会要求模型修正。明确指定的角色由能力约束绑定，不能由其他角色冒名完成。

代码任务只有取得 Reviewer 自己的成功 code.test 调用证据，且对应目标当前修订和 Artifact SHA，才能通过独立验收。缺证据、借用 Executor 的测试或返工后未复验时，运行显示部分完成并列出验收缺口。纯文档任务不强制代码测试。

当前 54 项回归已通过。另已完成脚本模型驱动的四角色真实工具联调：Research 抓取 CPython 官方资料，Executor 在 Docker 中执行及测试，Reviewer 在独立 Docker 中复核，只读 Artifact 逐步交接，Coordinator 生成最终报告。该联调仍不等同于真实模型全流程验收。
