# W13 - K3 板 SSH 连通，StarryOS 启动多 hart 适配

**周期**：2026-09-12 ~ 2026-09-19
**仓库**：StarryOS（`mul-hart-k3` 分支）

> 两件事。K3 板这边：W12 买的线材到齐，SSH 连上板子，验证了板子各项功能正常。这两周主要在进迭时空官网看资料、下载工具链、配置环境、熟悉操作，现在对板子有了基本的了解和使用经验。StarryOS 这边：单 hart 的故障恢复做完后，本周确定了多 hart 适配方案，UART 和网卡两个驱动一起改，工作量很大，目前第一阶段（同步与调度基础）进行中，记为后续工作。

## K3 板 SSH 连通

W12 自购的线材（一个串口线，三个杜邦按钮，一个电源，一个网线）本周到齐，接线后通过 SSH 连上板子，板子表现正常。

这两周的板子工作以熟悉环境为主：

- 在进迭时空官网阅读 K3 资料，继续补 [`daivy2333/k3`](https://github.com/daivy2333/k3) 文档仓的积累
- 下载工具链、配置开发环境
- 熟悉板子的基本操作

现在对板子有了基本的了解和使用经验，可以作为后续真板开发的起点。

## StarryOS：确定多 hart 适配方案

异步 UART 和网卡在 QEMU 单 hart 下已完成验证（UART 的 RX/TX copier 与 `flush/tcdrain`，网卡的队列 owner、stack runner、就绪状态和故障恢复），但这些结果都不能外推到多 hart。K3 的 AP 域静态有 8 × X100 + 8 × A100 共 16 个 hart，两个驱动要在真板上跑，就必须先解决多 hart 下的正确性问题。

9/16 确定改造方案，要点：

- 以 `SMP=16` 作为正式验证规模，对应 K3 AP 域数量；线程绑核从实际在线的 hart 集合计算，不硬编码 hart ID
- kernel critical-section 从"只关本 hart IRQ"改为"本地 IRQ restore + 全局 Acquire/Release 互斥 + per-hart 嵌套计数"
- 在工作区 vendor 一份 `axtask`，增加入队前绑核、安全绑核更新和远端就绪 IPI
- UART RX/TX copier 和网络 owner/runner 在 secondary hart 就绪后再以入队前绑核启动，保留 SPSC 单生产者单消费者和队列唯一 owner 的结构
- UART 和网络分开做固定绑核验证，再跑组合压力

当前进度：方案已确定，开发分支 `mul-hart-k3`，第一阶段（多核同步与调度基础：critical-section、axtask、IPI、PLIC 映射修正）进行中，工作区有未提交改动。

相关工作细节写成了一篇笔记：[为什么异步驱动要做多 hart 适配](../notes/异步驱动/multi-hart-adaptation-why-and-how.md)。

## 下周（后续工作）

- 继续推进多 hart 适配，因为要依次从串口开发到网卡，工作量较大，而且有很多问题只有测试才会暴露问题，姑且算作后面两周的任务
- K3 板继续熟悉，准备真板开发环境

## 参考

- [StarryOS（daivy2333/StarryOS）](https://github.com/daivy2333/StarryOS) — 多 hart 适配的设计与任务清单在本地 `mul-hart-k3` 分支的 `openspec/changes/ms08-qemu-multi-hart-correctness-baseline/` 目录下，尚未推送
- [K3 资料仓（daivy2333/k3）](https://github.com/daivy2333/k3)
- [笔记：为什么异步驱动要做多 hart 适配](../notes/异步驱动/multi-hart-adaptation-why-and-how.md)
