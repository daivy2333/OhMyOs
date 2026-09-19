# SMP：核心数量如何影响调度，多核为什么不能只靠调度器

**日期**：2026-09-16
**标签**：smp, riscv, scheduler, ipi, memory-ordering, k3

> 来源：QEMU 单核与真板多核差异的深度讲解。
> 范围：为什么单核调通不能直接在多核真板上让调度器自动多核调度。

## 结论

调度器只能在已经启动、初始化并加入调度域的 hart 之间分配任务。它不是 CPU 启动器，也不会自动修复中断、内存映射、锁、IPI 和驱动里的单核假设。

所以单核调通后直接烧多核真板，两种结果：

- 真板只启动一个 hart：仍按单核运行，但没利用多核。
- 真板启动多个 hart：SMP 基础没准备好时，要么在进调度器之前崩溃，要么运行一段时间后出现竞态、丢唤醒、死锁和队列损坏。

## 核心数量到底指什么

至少四个完全不同的数字：

| 概念 | 含义 |
|---|---|
| configured / max hart | 内核编译时最多支持多少 hart，例如 `MAX_CPU_NUM=16` |
| present hart | DT、固件或硬件描述里存在多少 hart |
| online hart | 已启动并完成 per-hart 初始化的 hart |
| schedulable hart | 调度器允许放置普通任务的 hart |

以 K3 为例：静态资料有 16 个 AP hart，但真板启动可能只有其中 8 个被固件拉起，也可能有 hart 被保留。调度器真正能用的只有最后一层，不是 datasheet 上的总数。

当前 StarryOS 构建里 `SMP=16` 同时影响六处：

1. `MAX_CPU_NUM` 等编译期配置。
2. 是否启用 SMP feature。
3. per-hart 数组、run queue、启动栈的大小。
4. QEMU 的 `-smp 16` 参数。
5. 内核要启动并等待多少个 secondary hart。
6. 中断控制器要映射多少个 hart context。

因此它不只是"告诉调度器有 16 个核"。

## 为什么从 1 核变 2 核是质变

从 8 核到 16 核大多是规模变化；从 1 核到 2 核是并发模型变化。

### 单核的"关中断"不再是互斥

单核：hart 0 关中断，本 hart 上的 task 和 ISR 都无法打断临界区，共享数据暂安全。

多核：hart 0 关自己中断，hart 1 仍可同时访问同一份数据。这正是当前 critical-section 的问题。本地关 IRQ 只能阻止本 hart 上的并发，阻止不了另一个 hart。

多核需要三件事叠加：

- 本地 IRQ 关闭
- 跨 hart 全局锁
- Acquire/Release 内存序

调度器不会自动给所有共享变量加锁。

### 唤醒另一个 hart 需要 IPI

假设 queue owner 固定在 hart 5 并进入 WFI 休眠，网络 IRQ 在 hart 11 到达。hart 11 把 owner 标记为 Ready，hart 5 仍在 WFI。

只把任务放入 hart 5 的 run queue，不一定能立刻唤醒它，通常还要发核间中断 IPI：

```
hart 11 → 提交共享状态 → 任务入 hart 5 run queue → 发 IPI
                                                          ↓
                                        hart 5 退出 WFI → 调度 owner
```

否则任务只能等下次 timer tick 偶然醒来。调度器可以选择任务，前提是有人让目标 hart 获得一次调度机会。

### 每个 hart 都需要独立初始化

一个 secondary hart 加入调度前，至少需要：

```
固件启动 hart
→ 分配独立启动栈
→ 初始化 per-CPU 数据
→ 设置页表和 trap 入口
→ 初始化本地 timer 和中断 context
→ 创建本 hart 的 idle task
→ 创建本 hart 的 run queue
→ 标记 online
→ 进入调度循环
```

任一步没完成，调度器都不能安全地把任务送过去。

当前 stack runner 可能在 secondary run queue 初始化完成前生成，而默认 affinity 允许它落到任意 hart，于是被分配到尚未初始化的 run queue。

### 中断控制器资源也跟 hart 数量有关

SMP=16 探针是现成的例子。当前 QEMU 平台只映射了约 `0x210000` 大小的 PLIC 区域，而 QEMU 选 boot hart 11，对应 supervisor interrupt context 23：

```
PLIC base + 0x200000 + 23 × 0x1000
= PLIC base + 0x217000     ← 超出旧映射到 0x210000
```

结果内核还没初始化调度器，就在 PLIC 初始化时 page fault。调度器无能为力，因为故障发生在它初始化之前。

### 单核掩盖内存序和竞态

单核上很多错误代码"看起来能运行"：写状态、唤醒任务、任务读状态，基本按时间顺序发生。

多核上写状态和唤醒可能发生在 hart 0，读取在 hart 1。缺少 Release/Acquire 关系时，hart 1 可能看见新的 generation，却仍读到旧状态字段，或丢失一次 register/wake 交错。这是共享内存协议问题，不是调度算法问题。

## 调度器能自动做什么

底层条件成立后，调度器能自动：

- 从 online run queue 选择任务。
- 根据 affinity 选择允许的 hart。
- 做负载分配。
- 任务阻塞时运行其他任务。
- 任务被唤醒后重新排队。
- 在允许的 hart 之间迁移任务。

不能自动完成：

- 启动 secondary hart。
- 初始化每个 hart 的 trap、timer、中断 context。
- 扩大 PLIC / IMSIC MMIO 映射。
- 判断某个 hart 是否被固件或 RP 系统保留。
- 给共享数据补锁和内存序。
- 保证驱动硬件 queue 只有一个 owner。
- 给远端 ready task 发 IPI（除非调度器已实现该机制）。
- 区分 K3 的 X100、A100、RT24，除非平台提供拓扑和能力信息。

## 能否先 QEMU 单核，真板再让调度器多核？

能，但要区分两个目标。

### 目标一：先让真板单核启动

完全可行，也是合理的 bring-up 顺序：

```
QEMU 单核通过
→ 真板只启动一个 AP hart
→ 验证串口、内存、timer、中断、驱动
→ 建立真板单核基线
```

此时 K3 即使有 16 个 AP hart，也只把一个 hart 加入 online 集合，调度器仍按单核工作。

### 目标二：真板自动使用多核

不能仅靠"真板多核，调度器自己处理"。需要先证明一组条件：

```
平台能启动所有目标 hart
→ 每个 hart 初始化完成
→ 调度器 run queue 可用
→ 跨 hart 锁正确
→ remote wake / IPI 正确
→ 驱动和 waker 没有单核假设
→ 实际 online 集合被正确报告
```

否则直接在真板打开多核，会把平台、内存、中断、调度器、驱动的问题混在一起，排障困难。

## 为什么计划看起来调整很多

不是因为"16 这个数字需要特殊代码"，而是当前系统只取得了单核资格，成熟 SMP 内核早已具备的基础设施还没补齐：

- secondary bring-up
- per-CPU 数据
- SMP-safe 锁
- IPI reschedule
- CPU affinity
- online / offline 管理
- 中断路由
- 内存序规则
- SMP-safe 驱动

现在做的不是按核数写多套适配，而是把配置和逻辑改成：

```
配置上限：16
实际可用集合：运行时获得
角色 placement：从实际集合动态计算
少于 3 个 hart：合法退化
多于 3 个 hart：多余 hart 留给普通调度
```

目标正是以后不再为 4 核、8 核、16 核分别适配。

## 对当前计划最准确的理解

SMP=16 有三层作用：

1. 容量检查：数组、mask、栈、PLIC 映射能否覆盖 K3 AP 域规模。
2. 并发检查：锁、waker、IPI、run queue 是否真正支持跨 hart。
3. 压力与边界检查：非零 boot hart、高编号 context、更多并发交错是否正常。

它不能证明 K3 真板本身，但能提前消灭与设备无关的 SMP 软件问题。

之后真板仍分两步：

```
K3 单 AP-hart bring-up
→ 确认 UART、内存、timer、AIA、网络基本路径
→ K3 多 AP-hart bring-up
→ 根据真实 DTB、固件/HSM、online mask 启用动态调度
```
