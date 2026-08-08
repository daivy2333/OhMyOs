# 异步 TX 侧四阶段 drain 与 fast retry

**日期**：2026-07-04
**标签**：rust, async, uart, drain, flush

> 来源：StarryOS `crates/uart_16550/src/async_/driver.rs:79-95`、`driver.rs:257-382`、`crates/uart_16550/src/async_/device_ops.rs:128-152`、`kernel/src/drivers/d1_uart.rs:157-182`。
> 范围：异步 TX 拷贝、四阶段 drain、fast retry、TEMT 自旋、QEMU/D1 真板 TX 差异。

## 答案

TX 是 RX 的镜像，方向相反。差别在四阶段 drain 与 TEMT 自旋。

## 关键常量

| 常量 | 值 | 位置 | 含义 |
|---|---|---|---|
| `TX_FAST_RETRY_LIMIT` | 32 | `driver.rs:31` | 单次 poll 内最多快速重试 |
| `TX_TEMT_POLL_LIMIT` | 256 | `driver.rs:33` | 等 TEMT 的最大自旋次数 |
| `COPIER_BUF_SIZE` | 1024 | `driver.rs:29` | copier 批量搬运缓冲 |
| `NAPI_THRESHOLD / BATCH` | 16 / 64 | `driver.rs:25-27` | RX 专用 |

## TX pipeline

```
write / writev             ← 应用层
  ↓ push
TX ring buffer             ← SPSC：多生产者 / copier 单消费者
  ↓ pop_batch（pop 也 wake）
TX copier task
  ↓ send_bytes
THR FIFO                   ← QEMU 16B / D1 64B
  ↓
wire
```

RX 的 `push_batch` 写完 wake 消费者（数据来了）。TX 的 `pop_batch` 读完 wake 生产者（空间出来了）。两边 SPSC 假设都成立。

## 四阶段 drain

`TxCompletion` 四个字段（`driver.rs:79-88`）：

| 字段 | 含义 |
|---|---|
| `ring_empty` | 生产者无新数据 |
| `copier_active` | copier 未在 poll |
| `staged_bytes` | pop 出但未塞 FIFO 的字节 |
| `transmitter_empty` | LSR TEMT，shift register 已排空 |

四者全 AND 才算 `is_drained`（`driver.rs:93-95`）。`tx_completion()` 用 `Acquire` 读后两字段，跨 hart 取得 copier 最新快照——这是 Q17 O63 修复点。

## fast retry

`tx_copier_loop` 内层循环（`driver.rs:294-329`）：

1. `send_bytes(&write_buf[cursor..pending])`
2. cursor ≥ pending → break
3. sent > 0 → retries=0；continue
4. retries+1；≤ 32 → continue
5. 超限 → 注册 `TX_WAKER` + 开 THRE IER + 最后一次 send_bytes
6. 仍无进展 → active=false；Pending

32 次覆盖 FIFO 抖动常态。立即 sleep + 等中断 + 调度代价远高于自旋 32 次。超 32 次认定「FIFO 卡死/硬件异常」，走「register waker + 开中断 + 让出」路径。

`tx_staged_bytes` 在此处累加/递减（`AcqRel`）。它告诉 `tcdrain`：「pop 出来但未塞 FIFO」的字节数。

## TEMT 角点

所有字节 send 完后，自旋 ≤ 256 次等 TEMT（`driver.rs:360-368`）：

```rust
if !self.uart.transmitter_empty() {
    for _ in 0..TX_TEMT_POLL_LIMIT {
        if self.uart.transmitter_empty() { break; }
        core::hint::spin_loop();
    }
}
```

THRE 与 TEMT 区别：

| 标志 | 含义 | 用途 |
|---|---|---|
| THRE | THR 寄存器可写 | copier 判断能否 send_bytes |
| TEMT | shift register 已排空 | tcdrain 等此 |

THRE=1 不等于 TEMT=1。最后一字节进 FIFO 后还在串行化。tcdrain 误判 THRE 就返回会丢 POSIX 语义。

256 次经验值：115200 baud 1 字节 ≈ 87µs，单核自旋 256 次在 µs 量级，足以等到。

## flush 的 register-then-recheck

`AsyncUartWriter::flush`（`device_ops.rs:128-152`）：

1. `tx_completion()`：已 drained → Ready
2. 否则注册 waker
   - 软件侧未完 → `tx.register_waker`
   - 硬件侧未完 → `DRAIN_WAKER.register`
3. 再 `tx_completion()` 检查（recheck 防 race）
4. drained → Ready；否则 Pending

M1 D3 顺序：先 register waker，再 recheck。避免 copier 在 register 之前 wake 导致永远等不到。

## QEMU vs D1 真板

| 维度 | QEMU NS16550 | D1 DW APB UART |
|---|---|---|
| THRE 触发 | 稳定每次触发 | 可能边沿丢失 / IIR 无 pending |
| D1 应对 | — | `update_ier(THR_EMPTY)` 后软件 wake `TX_WAKER` / `DRAIN_WAKER`（`d1_uart.rs:171-181`）|
| IER 写顺序 | — | IRQ-off 临界区保护 ier_cache RMW（Q17）|

QEMU 掩盖 THRE 边沿丢失。D1 真板上 64B 小包退化为 8.8% 线速，与此路径直接相关。

## 易混点

**THRE ≠ 发完**。THRE=1 表示 FIFO 可写，TEMT=1 才是物理层发完。

**四阶段不是同一件事查四次**。每次条件独立，任何一项不满足都不能返回。

**fast retry 不是空转浪费**。32 次自旋覆盖 FIFO 抖动常态，比 sleep+ISR 调度省得多。

**flush Pending 不是 bug**。register-then-recheck 是标准模式，Pending 是协作式让出。

**TX copier 不需要 NAPI**。TX 是发送方，不需要「中断合并」语义。

## 经验

- 四阶段 drain = POSIX tcdrain 在异步上下文的标准实现
- fast retry 是「吞吐/延迟」权衡旋钮，不是越大越好
- 评估 flush：是否覆盖四阶段？register-then-recheck 顺序对没？
- THRE/TEMT 不能互换；flush 必须等 TEMT
- 真板 quirk 在软件层补：D1 THRE 边沿丢失靠 software wake 兜底

## 补充

字节生命周期 6 状态：[1] 应用层 buf；[2] TX ring；[3] staged（copier 内 `write_buf`）；[4] UART THR FIFO；[5] 移位寄存器；[6] 线缆。

| 阶段 | 谁推进 | 何时确认完成 |
|---|---|---|
| 2→3 | TX copier（`pop_batch`）| 函数返回的 `n` |
| 3→4 | TX copier（`send_bytes`）| 函数返回的 `n`；FIFO 有空才接受 |
| 4→5 | UART 硬件自动 | 不可观测，靠 `THRE` 推断 |
| 5→6 | UART 硬件自动 | 仅 `TEMT` 可信 |

`THRE` 只说明 FIFO 不满（可继续 send_bytes）。`TEMT` 才说明移位寄存器已空（最后一字节离片）。

四阶段 drain 字段与字节状态对应：

| 字段 | 对应状态 |
|---|---|
| `ring_empty` | [1]→[2] 没新字节 |
| `!copier_active` | copier 不会从 [2] 取新字节 |
| `staged_bytes == 0` | [3] 无字节等进 FIFO |
| `transmitter_empty` | [4] FIFO 与 [5] 移位寄存器全空 |

四条件全 AND = 应用层之后无字节处在 [1]→[6] 任一阶段。

`copier_active` 真实语义：表示「copier 当前是否在 poll 内部」，不是「是否被调度」。

| 时机 | `tx_copier_active` |
|---|---|
| copier 进入 `poll_fn` 入口 | `store(true, Release)` |
| copier 在 `poll_fn` 内推进 | `true` |
| copier 在 `poll_fn` 内将 `return Pending` | `store(false, Release)` |
| copier 在 `poll_fn` 内返回 `Ready(())` | 保持 `true`（外层 loop 立刻再进 poll）|
| copier task 完全退出 | 不会发生（`tx_copier_loop` 是 `loop{}`）|

flush 等待 `!copier_active` 是「copier 已决定 sleep 等待 IRQ」。

flush 的两个 waker：flush 同时注册 `tx.register_waker` 与 `DRAIN_WAKER`，覆盖软件与硬件两侧：

| waker | wake 触发 | 含义 |
|---|---|---|
| `tx.register_waker` | `pop_batch` 时 `tx.poll.wake()`（`ring_buffer.rs:202-204`）| 软件路径还在前进 |
| `DRAIN_WAKER` | `isr.rs:99`、`d1_uart.rs:208`、`driver.rs:373` | TX 完成或即将 TEMT |

recheck 为什么必要：「register 之前刚好 wake 了」是一个 race：

```
t1: copier 完成最后 send_bytes，DRAIN_WAKER.wake()
t2: flush 调 tx_completion，未 drained
t3: flush 注册 waker（wake 已发生，无意义）
t4: flush recheck → drained → return Ready
```

不先 register 再 recheck：t1 wake → t2 flush tx_completion 未 drained → t3 注册 → 永远等不到。

QEMU vs D1 真板 drain 差异：

| 维度 | QEMU NS16550 | D1 DW APB UART |
|---|---|---|
| THRE 触发 | 稳定每次触发 | 边沿可能丢失 |
| TEMT 检测 | 正常 | 正常但 wake 路径不可靠 |
| 兜底 | — | `update_ier(THR_EMPTY)` 后 software wake（`d1_uart.rs:171-181`）|

QEMU 稳定；D1 不靠 software wake 兜底会 flush 永久 Pending。

64B 短包为何退化：长包 copier 持续 fast retry 内部掩盖 THRE 边沿丢失；短包每次进/出 poll 都有 THRE 边沿处理路径。Q19C 调研 64B 仅 8.8% 线速与此相关。D1 真板靠 `d1_uart.rs:171-181` 部分修复；QEMU 默认不开此路径，故 QEMU 退化为表象。

易错点扩展：

| 误判 | 真相 |
|---|---|
| `THRE=1` 就是发完 | `THRE=1` 只说明 FIFO 可写 |
| `ring_empty` 就够 | 软件侧空，硬件侧可能还在 |
| `flush` 立刻返回 = bug | Pending 是协作式让出 |
| `tx_copier_active` 表示调度态 | 表示「copier 是否在 poll 内部」|
| QEMU drain OK = D1 也 OK | D1 边沿丢失，必须 software wake 兜底 |