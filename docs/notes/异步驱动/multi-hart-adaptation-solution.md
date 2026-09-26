# 多 hart 适配是怎么落地的

**日期**：2026-09-26
**标签**：smp, multi-hart, async-driver, riscv, starryos, critical-section, ipi, scheduler

> 范围：StarryOS 的异步 UART（NS16550）和异步网卡（VirtIO-MMIO）两个驱动，QEMU `SMP=16`。
> 前置：[为什么异步驱动要做多 hart 适配](./multi-hart-adaptation-why-and-how.md) 列出问题，本文对应给出解法和代码位置。
> 依据：本地 `mul-hart-k3` 分支。QEMU 结果不外推到 K3 真板。

## 整体数据流

四件事按这个顺序接起来，后一件依赖前一件：

1. 每个 hart 启动时写下自己的运行队列，然后把自己登记进"已就绪位图"
2. 驱动角色从已就绪位图里挑 hart，把绑核在入队前钉死
3. 唤醒发生时，选目标队列 → 改状态 → 远端就发一次 IPI
4. 共享状态（waker、ring、快照）靠"本地关中断 + 全局 owner 锁"互斥

## 1. 临界区：本地关中断之外，加一把全局 owner 锁

### 原来的问题

kernel 的临界区只关本 hart 中断。Embassy `AtomicWaker` 的 waker cell 是非原子的读-改-写，它靠临界区提供**跨 hart** 互斥。单 hart 下每个 hart 独占自己的"临界区"，恰好成立；多 hart 下两个 hart 可以同时认为自己独占临界区，同时改同一个 waker cell。

### 现在的做法

`kernel/src/critical_section_policy.rs` 里的 `acquire()` 分三步（`:87`）：

```rust
let was_enabled = ops.irqs_enabled();
ops.disable_irqs();                       // 1. 关本 hart 中断，记下原状态
let prev = depth_mut(cpu).fetch_update(AcqRel, Acquire, checked_increment_depth)?;
if prev == 0 {                            // 2. 嵌套深度 0 → 1，才抢全局所有权
    while GLOBAL_LOCK.compare_exchange_weak(false, true, Acquire, Relaxed).is_err() {
        core::hint::spin_loop();          // 3. 抢不到就自旋
    }
}
was_enabled
```

`release()`（`:121`）对称：深度减一，1 → 0 时 `GLOBAL_LOCK.store(false, Release)`，然后**只有** `was_enabled` 为真才重新开中断。

三个设计点：

| 做法 | 为什么 |
|---|---|
| 全局只有一把锁 + per-hart 嵌套深度 | 临界区要保护的是同一组 waker cell，锁的粒度必须覆盖所有 hart。嵌套深度让同 hart 重入只加计数，不必反复抢 |
| 抢不到锁时自旋 | 临界区里只有几次原子操作，禁止阻塞、让出或拿驱动锁，所以不能用任何会挂起的锁 |
| `release(false)` 不开中断 | ISR 里 `acquire` 时中断已经是关的。恢复原状态而不是无条件开中断，才不会在中断还没处理完就提前重入 |

挂在 `critical_section` crate 的官方 `set_impl!` 上（`kernel/src/lib.rs:66`），`RawRestoreState` 直接用 `restore-state-bool`。这个文件刻意不依赖 `axhal`，宿主测试用 `#[path]` 把同一份代码连同假的 `IrqOps` 后端编进去，跑的是同一套 `acquire`/`release`。

失败路径一律 fail closed：hart id 越界、深度下溢、深度到 `u32` 上限，都 panic 而不是绕回去。

## 2. 每个 hart 一条运行队列，"已就绪"是单独的一个位图

### 队列数组

`RUN_QUEUES` 是长度 `MAX_CPU_NUM` 的 `[MaybeUninit<&'static mut AxRunQueue>]`（`crates/axtask/src/run_queue.rs:56`）。用 `MaybeUninit` 是因为不能让整个系统为"还没启动的 hart"付出初始化成本，代价是取槽位前必须先证明它写过了。

### 就绪位图

`SCHEDULABLE: AtomicUsize`（`:72`）是唯一的就绪事实来源：

```rust
// 写：先把槽位写好，再 Release 发布
unsafe { RUN_QUEUES[cpu_id].write(RUN_QUEUE.current_ref_mut_raw()); }
mark_schedulable(cpu_id);                    // fetch_or(1<<cpu, Release)

// 读：Acquire 读到就说明槽位已经写好
let bits = SCHEDULABLE.load(Acquire);
```

`init()`（`:782`，boot hart）和 `init_secondary()`（`:812`，副 hart）都走"写槽位 → 置位"这两步。Release/Acquire 这一对是这里的关键：读侧看到某位为真之后再去解引用 `RUN_QUEUES[cpu]`，写侧保证已经写完。

### 三个容易混的集合

| 名字 | 含义 | 用错的后果 |
|---|---|---|
| `axhal::cpu_num()` | 配置的 CPU 数量 | 只说明"硬件有几个"，不代表副 hart 的队列已初始化。用它做校验会放行未初始化的槽位 |
| `cpu_mask_full()` | 全 CPU 掩码 | 普通 spawn 的默认亲和性。任务第一次入队就可能被丢进任意一条队列 |
| `schedulable_cpu_mask()` | 已发布就绪的 hart 集合 | 绑核和分配只应该看这个 |

### 入队前绑核

`spawn_task_with_affinity()`（`crates/axtask/src/api.rs:239`）的顺序是：先 `validate_schedulable()` 校验，再 `set_cpumask()`，最后才 `select_run_queue()` 选队列入队。校验不过直接返回 `None` 且不入队，不会退回"随便找个队列"。

`set_cpumask_checked()`（`crates/axtask/src/task.rs:242`）是运行期改绑核的安全版本：掩码为空、越界或指向未发布 hart 时返回 `false`，旧掩码原样保留，并记一次 reject 计数。

## 3. hart 之间怎么通信：一次远端 IPI

### 通路

QEMU virt 上 `axhal::irq::send_ipi` 走 SBI `send_ipi`（hart mask）→ OpenSBI 给目标 hart 挂 supervisor software interrupt。guest 侧 `IPI_IRQ` 注册进 `S_SOFT` 槽位，handler 跑完由平台清 `sip::clear_ssoft()`。

### 单槽位所有权

`try_register_ipi()`（`crates/axtask/src/ipi.rs:101`）用 `IPI_REGISTERED` 做 CAS，保证全系统只有一个 S_SOFT 拥有者。注册失败时把标志位回滚，下一次还能再试。`init_scheduler()`（`api.rs:98`）在 boot hart 上注册，失败直接 assert——宁可启动失败，也不要出现第二个 IPI 拥有者。

### handler 做什么

`reschedule_ipi_handler()`（`ipi.rs:86`）只做两件事：记一次接收（含 per-hart 计数），给当前任务置抢占标志。**不切任务、不碰运行队列**。切换交给通用的中断退出路径，和 timer tick 走同一条路。

### 什么时候发

`should_notify_remote()`（`ipi.rs:71`）是纯判定：

```rust
was_ready && cpu_id != this_cpu_id
```

`was_ready` 只在 `Blocked → Ready` 真实成功时为真，所以重复唤醒、不在 `Blocked` 状态的唤醒都不会发 IPI。本地唤醒不发，只置抢占标志（`run_queue.rs:397`）。

## 4. 唤醒目标选择：本地优先

### 踩到的问题

UART TX copier 固定在远端 hart，startup 打完一个字节后 copier 最终恢复了，IPI 发送和接收计数一直是 0。原因是 `AxWaker` 复用了普通 spawn 的全局轮转来选目标，并以 `resched=false` 入队，远端入队不通知任何人。

直接把唤醒一律改成 `resched=true` 的尝试被完整回退：全掩码的网络协议栈 runner 会在 16 个 hart 之间被唤醒轮转，每次都发 IPI，形成 IPI 风暴。

### 最终规则

`select_wake_cpu()`（`run_queue.rs:145`）是纯函数：

```rust
let intersect = task & schedulable;
if intersect.is_empty() { return None; }                    // 失败关闭
if current < MAX_CPU_NUM && intersect.get(current) { return Some(current); }
intersect.first_index()                                      // 交集里最低的一个
```

即：当前 hart 同时在任务亲和集和已发布集合内就留在本地；否则从交集里确定性地取一个。两个后果正好对上前面两个问题——固定绑核的 copier 必然投递到它那一个远端 hart（发一次 IPI），全掩码任务留在本地（不发 IPI、不迁移）。

`AxWaker`（`crates/axtask/src/future/mod.rs:41`）走这个入口并请求真实的 `Blocked → Ready`。普通 spawn 仍走 `select_schedulable_cpu()` 的全局轮转，行为没变。

## 5. 绑核和分配：角色到 hart 的确定性映射

### 规则

`kernel/src/drivers/placement.rs:103` 的 `place_roles()` 接收一个升序、去重、无越界的已发布 hart id 列表和一个 anchor：

```
at(i) = schedulable[(anchor 在列表中的位置 + i) % len]
角色 i：UART RX copier = 0，UART TX copier = 1，网卡 owner = 2，网卡 runner = 3
```

输入不合法（空、乱序、重复、任一 id ≥ `MAX_CPU_NUM`）返回 `None`，调用方 fail closed。anchor 不在列表里时从索引 0 开始——不虚构拓扑过滤。

规则是纯函数，不碰 MMIO、IRQ 路由和 hart 数量连续性，所以能在宿主测试里穷举。`SMP=16` 下四个角色落在四个不同 hart 上；只有单 hart 时才共置。

### 启动顺序

| 角色 | 启动点 | 顺序要求 |
|---|---|---|
| UART RX/TX copier | `uart_init::start_copiers()`（`:463`） | 硬件初始化之后、startup benchmark 之后。`RX_COPIER_STARTED`/`TX_COPIER_STARTED` 拒绝第二次启动 |
| 网卡 stack runner | `axnet::start_stack_runner_affinity()`（`crates/axnet/src/stack_runner.rs:683`） | Service 先装好。runner 起不来是硬停止，不允许 owner 在没有 runner 的情况下启动 |
| 网卡队列 owner | `start_rx_task_affinity()`（`crates/axnet/src/async_rx.rs:3078`） | IRQ 注册成功之后才起 |

pinned 的 hart 只在 spawn 真正成功后记录（`record_runner_pinned` 在 `Ok` 分支里），spawn 被拒时观测里显示 UNKNOWN 而不是假角色。

### 失败原子性

`start_affinity_with_checked()`（`stack_runner.rs:658`）保证失败的 spawn 不消耗掉"只能启动一次"的生命周期：先按已发布集合校验 → CAS 生命周期 → spawn → 只有拿到句柄才提交，`None` 时把生命周期回滚，下一次还能重试，且不会重复入队。

### 迁移

不改结构，只改掩码：把某个角色的掩码从单 hart 扩到两个 hart，它会在下一次自然阻塞/唤醒时落到另一个 hart，再收回单 hart。全程不新建第二个 copier/owner，UART ring 保持单生产者单消费者，网络保持每队列唯一 owner。目标非法时失败并保留原掩码。

## 6. 顺带修掉的三个缺陷

### UART TX copier 丢唤醒

copier 在 ring 空时注册 waker、发布 inactive、返回 `Pending` 停放。如果生产者在这两步之间推入数据，任务状态还是 `Running`，没有 `Blocked → Ready`，那次唤醒就丢了，随后停放在一个非空 ring 上。表现是同一个二进制时而 PASS 时而失败。

修法是补上"注册→重查"：注册 ring waker 后重查 ring，非空就 self-wake 重试，不停放（`crates/uart_16550/src/async_/driver.rs`），并加 `park_after_register_retry` 计数。

### release 构建越界读出幽灵 hart

绑核代码按硬编码 64 遍历 `CpuMask<16>`，而 `cpumask 0.1.0` 的索引 `get`/`set` 只有 `debug_assert` 保护。release 下越界读返回垃圾 `true`，于是并不存在的 hart 16 被当成可调度。

修法是 `crates/axtask/src/cpumask.rs` 的 `AxCpuMask` 新类型：索引访问在 debug 和 release 都做容量检查，越界读报"不在集合"，越界写返回 `AxCpuMaskError::IndexOutOfRange` 且掩码不变。registry 类型不再从公开 API 露出（无 `Deref`、无 `AsRef`、拿不到内部值）。所有掩码容量统一取 `axconfig::plat::MAX_CPU_NUM`，不再各自硬编码。

### PLIC 窗口不够 16 个 hart

QEMU 平台配置里 PLIC MMIO 写成 `0x0c00_0000/0x21_0000`，只够 8 个 hart 的 supervisor context。16 hart 实测中 boot hart 11 在 `init_percpu` 访问超映射地址时 page fault，后面的 `current task is uninitialized` panic 把原始故障盖掉了。`.axconfig.toml:22` 改成 `0x0c00_0000/0x60_0000`（设备树给的就是 6 MB）。

## 7. 怎么证明它真按这个跑

- **UART 快照**：QEMU-only 命令 `0x55534d31`，152 字节定长帧。内容有配置/在线掩码、两个 copier 的绑核、实际中断 hart、copier 最近与累计 hart、ring 占用与空位、TX 四阶段完成、远端入队/IPI/恢复计数、非法绑核拒绝数。逐字节序列化而不是 memcpy 结构体，否则 `repr(C)` 填充的未定义字节会带进用户态。旧 TXDBG 接口不变
- **网卡 V5**：`0x4e495435`，按字节是 V4 的前缀扩展，V1–V4 的命令、布局、语义不动。关键区别是 V5 里 owner/runner 的 hart 是任务在轮询入口记下的**实际值**，不是从绑核掩码推断的
- **关 timer 的远端唤醒见证**：目标 hart 关掉本地 timer 再停放，另一个 hart 触发唤醒。如果任务还能被救活并记录到自己 hart 上的 IPI 接收增量，就排除了"靠 100Hz tick 救活"的可能。见证是单飞的，每次运行重置状态，取消和超时都要求目标先恢复 timer 才允许发布终态

## 8. 还没做完的

- **迁移视图的读写序**：`MigrationSlot` 用原子字段加自定义 seqlock。写侧在奇数标记后加了 Release fence，但 Release 只约束它之前的操作，后续字段写仍可能越过；读侧在最终序号检查前也少一道边界。弱内存序下读者可能接受混合视图。修法已定，代码还没改
- **SMP=16 上的分驱动运行验证**：串口单独一遍、网卡单独一遍，各自独立判定，不以另一方成功替代
- **组合压力与恢复交错**：两驱动同时跑，以及网卡 reset/link 翻转时 UART 仍有可判定进度

## 代码位置

| 主题 | 位置 |
|---|---|
| 临界区策略 | `kernel/src/critical_section_policy.rs:87`（acquire）、`:121`（release） |
| 临界区接线 | `kernel/src/lib.rs:66` |
| 运行队列与就绪位图 | `crates/axtask/src/run_queue.rs:56`、`:72`、`:782`、`:812` |
| 唤醒目标选择 | `crates/axtask/src/run_queue.rs:145`、`crates/axtask/src/future/mod.rs:41` |
| 远端 IPI | `crates/axtask/src/ipi.rs:71`、`:86`、`:101` |
| 入队前绑核 | `crates/axtask/src/api.rs:239`、`crates/axtask/src/task.rs:242` |
| 掩码容量安全 | `crates/axtask/src/cpumask.rs` |
| 角色分配 | `kernel/src/drivers/placement.rs:103` |
| UART copier 启动 | `kernel/src/drivers/uart_init.rs:463` |
| 网卡角色启动 | `kernel/src/drivers/virtio_net_irq.rs:168`、`crates/axnet/src/stack_runner.rs:683`、`crates/axnet/src/async_rx.rs:3078` |
| 观测命令 | `kernel/src/syscall/fs/ctl.rs` |

## 边界

- 迁移只改变同一任务的执行 hart，不创建第二实例
- QEMU `SMP=16` 只证明 16 个同构 hart 的软件并发。X100/A100 异构能力、AIA 中断路由、真板实际在线的 hart 集合留给真板验证
- 不包含 CPU 热插拔、IRQ 动态负载均衡、多队列网卡和多网卡
