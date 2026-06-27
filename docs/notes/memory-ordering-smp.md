# 内存序：QEMU 掩盖的真板陷阱

**日期**：2026-06-25
**标签**：rust, memory-ordering, riscv, smp, atomic, optimization

> 来源：第 2 站 Q3 + O63 条目 + 深度讲解。
> 范围：Q15 用 `Relaxed` 的字段在多核真板下的隐患。

## 背景

QEMU 单 hart：`Relaxed` 够。真板多核（SMP）：必须升级到 `Acquire/Release`。

QEMU 模拟单 hart。所有"并发"实际是单线程事件循环，没有真正的跨 CPU 同步需求。

VisionFive2 是 4 核 RISC-V。多核并行下：

```
hart 0: TX copier 写 tx_staged_bytes += N
hart 1: flush() 读 tx_staged_bytes
        ↑ 可能看到陈旧值（cache 未同步）
```

## 内存序概念

内存序解决"代码书写顺序"与"实际执行顺序"不一致的问题。

### 重排的两个来源

| 来源 | 谁做的 | 目的 |
|---|---|---|
| 编译器重排 | LLVM | 优化冗余加载、提升缓存命中 |
| CPU 重排 | 处理器流水线 | 隐藏内存延迟，提升吞吐 |

单线程下重排无影响（结果等价）。多线程下可能让其他线程看到不一致的中间状态。

### 朴素假设（错误）

```rust
data = 42;
ready = true;
```

朴素想法：另一线程看到的顺序一定是 `data=42` → `ready=true`。

实际：CPU/编译器都可能重排。`ready=true` 可能在 `data=42` 之前可见。

## 三个架构对比

| 架构 | 模型 | 重排限制 |
|---|---|---|
| x86 | TSO（强）| 同地址读写有序；不同地址写写有序 |
| ARM | 弱 | 几乎所有读写都可重排 |
| RISC-V | RVWMO（弱但比 ARM 友好）| 同地址有序；其他可重排 |

**TSO**：所有线程看到的写顺序一致（同一地址）。

**RVWMO**：弱序，但有地址依赖等保证。比 ARM 友好。

## Rust 的 5 种 Ordering

| Ordering | 保证 | 性能 |
|---|---|---|
| Relaxed | 仅原子性 | 最快 |
| Acquire | 后续读写不重排到此读之前 | 快 |
| Release | 之前读写不重排到此写之后 | 快 |
| AcqRel | Acquire + Release | 中 |
| SeqCst | 全序一致 | 慢 |

## Acquire/Release 配对

最常用的模式：

```
写端（Release）              读端（Acquire）
───────────────────         ───────────────────
val = 42;                   while (!ready) {}  // Acquire load
ready = true;  // Release   print(val);        // 必看到 42
```

配对保证：
- Release 之前的写对 Acquire 之后可见
- Acquire 看到的值包含 Release 之前的所有写

### RISC-V 指令对应

```asm
# Release 写
fence rw, w
sd t0, 0(a0)

# Acquire 读
ld t0, 0(a0)
fence r, rw

# AcqRel RMW
fence rw, rw
amoadd.d.aqrl t0, a1, 0(a0)
fence rw, rw
```

## RISC-V 友好性

- RVWMO 比 ARM 弱序稍强（地址依赖等保证）
- fence 指令 ~1-5 ns 开销
- 比 ARM 更适合 OS 开发
- Q15 的 134µs 1B e2e 基线不受影响（noise 范围内）

## 升级清单

| 位置 | 当前 | 升级 |
|---|---|---|
| `uart_init.rs:106, 109` `ier_cache` | Relaxed | Release/Acquire |
| `driver.rs` `tx_copier_active` | Relaxed | Release/Acquire |
| `driver.rs` `tx_staged_bytes` | Relaxed | AcqRel |
| `driver.rs:169-180` `tx_completion` | Relaxed | Acquire |

补充：全局 grep `Ordering::Relaxed` 评估其他点。

## 升级原则

- 写端 store：Relaxed → Release
- 读端 load：Relaxed → Acquire
- RMW（fetch_add/sub）：Relaxed → AcqRel
- 单 hart 纯诊断字段：保留 Relaxed

## 关联模块（已正确）

- `embassy_sync::AtomicWaker`（库实现用 AcqRel）
- `embassy_hal_internal::atomic_ring_buffer`（库实现用 AcqRel）

## 真板症状

- `staged_bytes` 漂移
- flush 过早返回
- tcdrain 不返回
- 偶发 panic

## 与 SpinNoIrq 关系

`SpinNoIrq::lock()` 内部用 `csrr/csrc` 是 RMW 原子操作，**隐含 Acquire/Release 语义**。

```rust
let guard = spin_no_irq.lock();  // 隐含 Acquire
guard.deref_mut().set_ier(...);  // 写入
// drop guard → Release：所有写入对其他线程可见
```

走 SpinNoIrq 的路径内存序自动正确。问题在绕过 SpinNoIrq 的手动 Relaxed 用法（如 `ier_cache`）。

## 怎么发现问题

| 方法 | 用途 |
|---|---|
| grep `Ordering::Relaxed` | 静态扫描 |
| ThreadSanitizer (TSan) | 编译期插桩，运行时检测竞争 |
| loom | Rust 形式化并发模型测试 |
| 真板 stress test | 多核同时跑 |

## 完整规范

详见 `optimization/spec.md` 的 Q6 章节 O63 条目（含修改清单与 Scenarios）。