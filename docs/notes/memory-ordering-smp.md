# 内存序：QEMU 掩盖的真板陷阱

**日期**：2026-06-25
**标签**：rust, memory-ordering, riscv, smp, atomic, optimization

> 来源：第 2 站 Q3 + O63 条目。
> 范围：Q15 用 `Relaxed` 的字段在多核真板下的隐患。

## 直接答案

QEMU 单 hart：`Relaxed` 够。
真板多核（SMP）：必须升级到 `Acquire/Release`。

## 为什么 QEMU 不出问题

QEMU 模拟单 hart。所有"并发"实际是单线程事件循环。没有真正的跨 CPU 同步需求，`Relaxed` 够用。

## 真板情况

VisionFive2 是 4 核 RISC-V。多核并行下：

```
hart 0: TX copier 写 tx_staged_bytes += N
hart 1: flush() 读 tx_staged_bytes
        ↑ 可能看到陈旧值（cache 未同步）
```

## RISC-V 内存模型

RISC-V 用 RVWMO（弱内存序）：

| 序 | 性能 | 适用 |
|---|---|---|
| Relaxed | 最快 | 单 CPU / 容忍过期 |
| Acquire/Release | 中等 | 跨线程同步 |
| SeqCst | 最慢 | 全局顺序 |

RISC-V 的 fence 几 ns，热点路径可接受。

## 影响位置（O63）

| 位置 | 当前 | 升级 |
|---|---|---|
| `uart_init.rs:106, 109` `ier_cache` | Relaxed | Release/Acquire |
| `driver.rs` `tx_copier_active` | Relaxed | Release/Acquire |
| `driver.rs` `tx_staged_bytes` | Relaxed | AcqRel |
| `driver.rs:169-180` `tx_completion` | Relaxed | Acquire |

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

## 修改清单（O63）

1. `uart_init.rs:106, 109` - ier_cache 改 Acquire/Release
2. `driver.rs` 所有 `tx_copier_active` 操作
3. `driver.rs` 所有 `tx_staged_bytes` 操作
4. `driver.rs:169-180` `tx_completion` 读改 Acquire
5. 全局 grep `Ordering::Relaxed` 评估

## 性能影响

RISC-V fence 几 ns。Q15 的 134 µs 1B e2e 基线不会被显著影响。

## 完整规范

详见 `optimization/spec.md` 的 Q6 章节 O63 条目（含修改清单与 Scenarios）。