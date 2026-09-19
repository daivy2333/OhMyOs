# 为什么异步驱动要做多 hart 适配

**日期**：2026-09-19
**标签**：smp, multi-hart, async-driver, riscv, starryos, k3

> 范围：StarryOS 的异步 UART（NS16550）和异步网卡（VirtIO-MMIO）两个驱动。
> 依据：StarryOS 多 hart 适配的设计与方案文档（2026-09-16 确定）。

## 背景

StarryOS 的异步 UART 和网卡驱动在 QEMU 单 hart 下已经做完：UART 有 RX/TX copier 和四阶段 `flush/tcdrain`，网卡有唯一队列 owner、stack runner、就绪状态桥接和设备恢复语义。这些验证都只覆盖单 hart，不能外推到多 hart。

目标是 K3 板。K3 静态资料显示 AP 域有 8 × X100 + 8 × A100 共 16 个 hart。两个驱动要在真板上跑，就必须先解决多 hart 下的正确性问题，这就是多 hart 适配工作的原因。

## 不做适配会遇到什么问题

### 1. critical-section 只关本 hart IRQ，waker 需要的全局互斥被破坏

kernel 的 critical-section 目前只关闭当前 hart 的 IRQ。UART 和网络都使用 Embassy `AtomicWaker`（由 `PollSet` 间接使用），它依赖 critical-section 提供**全局**互斥来保护非原子的 waker cell。

单 hart 下这不构成问题。多 hart 下，一个 hart 在临界区内更新 waker cell 时，另一个 hart 仍可进入自己的"临界区"并并发 register/wake，全局互斥不再成立。竞态窗口真实存在：UART ISR、copier 和 TTY 读写方分处不同 hart 时，都会触达同一组 `RX_WAKER`/`TX_WAKER`/`DRAIN_WAKER`。

### 2. 后台任务首次入队前没有绑核，可能落进未初始化的运行队列

`axtask` 只允许任务入队**之后**设置绑核，普通 spawn 用全 CPU mask 选择运行队列。两个问题：

- stack runner 在 secondary hart 的运行队列初始化完成**之前**就生成了，可能解引用未初始化的 `MaybeUninit` 运行队列。
- `axhal::cpu_num()` 返回的是配置数量，不代表 secondary scheduler 已就绪，不能用它做校验。

UART copier 通过 `OsRuntime::spawn` 立即普通入队，`ArceOsRuntime` 只调用 `axtask::spawn_with_name`，无法在首次运行队列选择前提交绑核。

### 3. 远端唤醒不发 IPI，就绪任务要等下一个 timer tick

`AxWaker` 每次 wake 重新选择运行队列，远端唤醒不发 IPI。目标 hart 上的就绪任务可能挂到下一次 100Hz timer tick 才被调度。这不只是延迟问题：异步唤醒的语义是事件驱动，靠周期 tick 救活意味着丢唤醒，事件路径实际未经验证。

### 4. SMP=16 直接启动会在早期触发 page fault

QEMU 平台配置把 PLIC MMIO 窗口写成 `0x0c00_0000/0x21_0000`，只够 8 个 hart 的 supervisor context。16-hart 实测中 boot hart 11 执行 `init_percpu` 时访问超出映射的 PLIC 地址触发早期 page fault，随后的 panic（`current task is uninitialized`）掩盖了原始故障。只缩回 8 hart 会掩盖这个映射缺口，达不到目标规模。

### 5. 只改网卡不改 UART，同一组全局原语仍有竞态

如果只让网卡做多核适配，UART ISR、copier 和 TTY 读写方之间仍共享同一组 waker 原语，竞态照旧存在，也建立不了 StarryOS 异步 I/O 的多 hart 基线。所以两个驱动一起纳入适配范围。

## 打算怎么解决

改造方案按层拆解（设计文档 D0–D9 决策的摘要）：

| 问题 | 方案 |
|---|---|
| PLIC 窗口不足 | 修正 QEMU 平台配置：PLIC MMIO range 改为 `0x0c00_0000/0x60_0000`，覆盖 16 hart context（D0） |
| 调度器缺绑核/IPI | 工作区 vendor 一份 `axtask 0.3.0-preview.2`，增加入队前绑核 spawn、安全绑核更新、在线集合的 Acquire/Release 发布和远端就绪 IPI；不动 Cargo registry（D1、D2） |
| critical-section 无全局互斥 | 改为"本地 IRQ restore + 全局 Acquire/Release 锁 + per-hart 嵌套深度"，保留 `restore-state-bool` ABI（D3） |
| 角色落核不确定 | kernel 层加纯绑核策略：输入在线 hart 集合，输出 UART RX/TX copier、网络 owner、runner 四个角色各自的单核绑核；不硬编码 hart ID（D4） |
| 启动时机过早 | 设备初始化与后台任务启动解耦：secondary hart 就绪后由 kernel adapter 以入队前绑核启动 copier/runner 并保存句柄（D5） |
| 行为无法直接观测 | UART 用独立的快照与控制接口，网络在旧快照格式后追加字段（旧格式字节级兼容），直接观测绑核结果、IRQ/任务所在 hart、IPI 因果；另加关闭 timer 的远端唤醒测试，排除 tick 救活的假象（D6、D7） |
| 迁移需求 | 通过扩展绑核 mask 加自然 block/wake 完成受控迁移，不新建第二实例（D8） |
| 验证顺序 | `SMP=16` 按"基础原语 → 各驱动固定绑核 → 迁移 → 组合压力与恢复交错"分阶段验证，任一阶段失败即停（D9） |

## 边界

- 迁移只改变同一任务的执行 hart，不创建第二 copier/读写方/owner；UART ring 维持单生产者单消费者结构，网络维持每队列唯一 owner。
- QEMU `SMP=16` 只证明 16 个同构 hart 的软件并发。X100/A100 异构能力、AIA 中断路由、真板实际在线的 hart 集合留给后续真板验证，本次改造不宣称 D1/K3 真板结论。
- 不包含 CPU 热插拔、IRQ 动态负载均衡、多队列网卡和多网卡。
