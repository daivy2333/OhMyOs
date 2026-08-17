# AgentOS 项目概览

> **当前阶段**：概念构想与前期调研  
> **方向**：Agent × Operating System × Rust × RISC-V

## 1. 项目背景

LLM 与 AI Agent 已从聊天程序演化为能够长期运行、使用工具、访问外部资源、维护状态并自主完成任务的软件实体。

绝大多数 Agent 仍运行在 Linux、Windows、macOS 等传统 OS 之上。所谓 "Agent OS" 也有相当一部分实际是传统 OS 之上的 Agent Runtime 或工具管理平台。

2026 年已经出现专门讨论 Operating Systems for AI Agents 的 AgenticOS Workshop（首届 ASPLOS 2026，第二届计划 SOSP 2026），议题包括 Agent execution abstraction、隔离、调度、checkpoint、memory、security、observability 等 OS 层问题。

IBM Research 的最新工作将当前阶段类比为 POSIX 和 Kubernetes 出现前的探索期：大量框架与实现已经出现，但 Agent 系统真正稳定的核心抽象尚未形成共识。

因此 Agent 与操作系统的结合是已经出现明确研究活动、但整体仍处于早期探索阶段的前沿方向。

## 2. 我们的设想

构建一个**以 Agent 为主要交互对象和运行主体的实验性操作系统**。

当前倾向使用 Rust + RISC-V，候选基础为 StarryOS / ArceOS。这些都还属于设想，最终架构并未确定。

使用方式上想打破传统 PC 假设。传统 OS 启动后通常进入：

```text
Boot → Login → Shell / Desktop → Application
```

我们设想 AgentOS 更接近：

```text
Boot → Agent Ready → User ↔ Agent
```

即**启动 OS 本身就相当于启动 Agent**。用户默认面对的不是 `root@machine:~#`，而是 Agent 的输入界面：

```text
AgentOS ready.

> 检查一下当前系统的网络状态
```

此时用户输入的默认含义不再是 Shell command，而是发送给 Agent 的自然语言请求。

### 2.1 Agent 是默认交互入口

用户描述"想完成什么"，由 Agent 决定如何利用系统能力。

传统终端不删除。保留一个明确的切换入口：

```text
:shell
```

进入传统终端 `agentos:~#`，用于开发、调试、系统维护、故障恢复、传统命令行操作；`exit` 后回到 Agent。

由此 AgentOS 同时存在两条交互路径：

| 入口 | 触发 | 用途 |
|---|---|---|
| Agent | 默认 | 普通用户任务 |
| Shell | `:shell` 切换 | 开发、调试、维护、恢复、紧急操作 |

至于 Shell 保留多少功能、Agent 与 Shell 的最终关系都没有必要提前确定。

## 3. 真正希望探索的问题

本项目不是要证明"传统 Linux 已经过时"，也不是简单"在 StarryOS 上跑一个聊天机器人"。

真正想回答的问题：

> 如果未来计算机主要运行 Agent，操作系统应该是什么样子？

具体方向：

- Agent 与传统 Process 的关系
- Agent 是否应成为一等执行实体
- Agent 如何访问系统资源
- Agent 如何安全地使用工具
- Agent 长期状态由谁管理
- 多 Agent 如何共存
- Agent 如何与硬件交互
- 自然语言能否成为主要交互方式
- 传统 OS abstraction 是否仍完全适合 Agent workload

这些问题都不预设答案。项目价值在于真正阅读与运行现有方案后，再通过自己的 OS prototype 去验证与推进。

## 4. 相关工作

被称为 "Agent OS" 的项目大体分三类。

### 4.1 Agent Runtime / OS-like Runtime

借用 OS 思想，但运行于传统 OS 之上。

- **AIOS** — 较早系统化提出 Agent Operating System 概念的项目之一。重点研究 scheduling、context、memory、storage、tool、Agent SDK；本质仍是传统 OS 之上的 runtime layer。
  - 仓库：<https://github.com/agiresearch/AIOS>
  - 论文：<https://arxiv.org/abs/2403.16971>
- **Agent libOS** — 将 Agent 抽象为 `AgentProcess`，研究 capability、tool、checkpoint、child agent、memory、audit。作者明确说明运行于传统 Host OS 之上，不是硬件操作系统。
  - 仓库：<https://github.com/yingqi-z20/Agent-libOS>
  - 论文：<https://arxiv.org/abs/2606.03895>
- **OpenFang** — Rust 写的开源 Agent runtime/system，自称 Agent Operating System，重点面向 long-running autonomous agents、tool、security、memory、multi-agent。运行在 macOS / Linux / Windows 上，与裸金属 AgentOS 不是一个层次的问题。
  - 仓库：<https://github.com/RightNow-AI/openfang>
  - 官网：<https://openfang.sh/>

### 4.2 真正接近 Agent-native OS 的项目

这一类与本项目设想更接近，因为它们真正涉及 kernel、bare metal、hardware abstraction。

- **agentOS** — Jordan Hubbard 发起，基于 seL4 Microkit。定位为可启动的面向 AI Agent 的 OS，覆盖 seL4 microkernel、capability security、Agent isolation、OS services、VM / guest OS。
  - 仓库：<https://github.com/jordanhubbard/agentos>
- **Fable-OS** — 理念与本项目最接近：自然语言为主交互，模型直接调用 kernel-level tools / syscals 而不是让 Agent 在 Linux 上执行 Shell。
  - 仓库：<https://github.com/robiot/fable-os>
- **Oxide OS** — 从零写的 Rust agent-native microkernel，标语 "Agents are kernel primitives, not userspace processes"。已能通过 QEMU 启动，正在探索 capability security、agent isolation、IPC、hardware drivers。
  - 仓库：<https://github.com/gkganesh12/oxide-os>

### 4.3 Agent 与传统 OS 结合的系统研究

针对 Agent 在传统 OS 上的资源与状态行为的研究。

- **AgentCgroup** — 研究 AI Agent 的 resource pattern，发现 tool call 产生明显且难以预测的 CPU / memory pattern，并使用 eBPF、cgroup、sched_ext 做 Agent-aware resource management。
  - 论文：<https://arxiv.org/abs/2602.09345>
- **Crab** — 研究 Agent sandbox 的 checkpoint / restore。Agent 状态不仅在 conversation history 中，也在 filesystem、process、runtime、sandbox 中；通过观察每轮执行的 OS side effect 决定何时真正需要 checkpoint。
  - 论文：<https://arxiv.org/abs/2604.28138>

## 5. 重点关注的入口

### Workshop

AgenticOS Workshop（Operating Systems Design for AI Agents）：<https://os-for-agent.github.io/>

首届 ASPLOS 2026，第二届计划 SOSP 2026。议题覆盖 Agent workload 所需的 OS primitives、isolation、scheduling、observability。

### 论文

- **Towards an Agent Operating System** — <https://arxiv.org/abs/2607.25076>。探讨 Agent computing 是否最终需要类似 POSIX / Kubernetes 的统一 abstraction；论文认为当前仍处于框架与 protocol 并存、核心 abstraction 未形成的探索阶段，与本项目位置契合。
- **Agent Operating Systems** — <https://arxiv.org/abs/2606.01508>。讨论传统 process、thread、syscall、file、permission 等 OS abstraction 在面对 long-running、goal-directed、probabilistic Agent 时可能遇到的问题。

## 6. OS 基础

| 项目 | 仓库 | 定位 |
|---|---|---|
| StarryOS | <https://github.com/Starry-OS/StarryOS> | 基于 ArceOS 的 experimental monolithic OS，支持 RISC-V 64 |
| ArceOS | <https://github.com/arceos-org/arceos> | Rust 写的 modular OS / unikernel，StarryOS 的底层基础 |

StarryOS 因团队已有相关积累，是比较自然的实验起点；是否最终沿用要在项目真正开始后再决定。ArceOS 更模块化，也值得与 StarryOS 一起纳入调研。

## 7. 当前项目定位

截至 2026 年 8 月：

> 一个面向 Agent-native Operating System 的前期探索项目。

当前设想：

1. 启动后默认进入 Agent，不是 Shell。
2. 默认通过自然语言与 Agent 交互。
3. 保留 Shell 作为开发、调试、高级操作入口。
4. Agent 不止是 OS 上的普通聊天程序，目标是探索 Agent 与 OS 更深层次的结合。
5. 倾向使用 Rust + RISC-V，可能以 StarryOS / ArceOS 作为初始实验基础。
6. 系统架构、Agent 模型、kernel interface、安全模型、memory、tool、调度均未定。

下一阶段真正要做的：

> 调研，不是提前设计。

需要实际阅读论文、运行开源项目，理解它们的设计动机与限制，才能判断本项目应采用的架构。这份概览只是后续探索的起点，不是最终设计方案。
