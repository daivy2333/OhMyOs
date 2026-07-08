# VFS 接口 + flush 实现路径

**日期**：2026-07-04
**标签**：rust, vfs, tty, flush, syscall

> 来源：StarryOS `kernel/src/syscall/fs/ctl.rs:43-67`、`kernel/src/syscall/fs/io.rs:125-130`、`kernel/src/pseudofs/dev/tty/mod.rs:107-220`、`kernel/src/file/mod.rs:139-181`、`crates/uart_16550/src/async_/device_ops.rs:128-152`、`crates/uart_16550/src/tty.rs:236-260`。
> 范围：flush 在 VFS / TTY / syscall 三层的语义差异、TCSBRK 双份实现反模式、Q22 维护性清理候选。

## 结论

flush 不是单一动作。它在「应用层 → 用户库 → syscall → VFS → TTY → driver → 硬件」链上每环语义不同。StarryOS 的 tcdrain 在 syscall 层硬编码拦截 TCSBRK，并复制了一份 register-then-recheck。

## 五层 flush 语义

L1 用户态 C 库：

| 调用 | 含义 |
|---|---|
| `fflush(stdout)` | 把 stdio 缓冲写到内核 fd，**不等**发送 |

C 库 fflush 只覆盖用户态缓冲。`fwrite + fflush` 只保证 hello 进内核 TTY 缓冲，不保证离开芯片。

L2 POSIX 系统调用：

| syscall | 语义 |
|---|---|
| `tcdrain(fd)` | 等输出排空到物理介质 |
| `fsync(fd)` | 等所有缓冲（含 metadata）持久化 |
| `fdatasync(fd)` | 等数据持久化（不含 metadata）|
| `ioctl(TCSBRK, 1)` | 发 break，等 drain |

tcdrain 与 fsync 对 TTY 实际等价。

L3 内核 VFS：`sys_fsync`（kernel/src/syscall/fs/io.rs:125）调 `File::from_fd(fd)?.inner().sync(false)`。只对 axfs::File 走 PageCache 同步。

TTY 节点不走 axfs::File。flush 走 ioctl 路径：`FileLike::ioctl` → `Location::ioctl` → `Tty::ioctl`。

L4 TTY 抽象：关键发现——`Tty<R, W>` 与 `TtyWrite` trait 都**没有** flush 方法：

```rust
// crates/uart_16550/src/tty.rs:252-260
pub trait TtyWrite: Send + Sync + 'static {
    fn write(&self, buf: &[u8]) -> usize;
}
```

flush 不在 Tty 抽象里。

L5 驱动层：`AsyncUartWriter::flush`（crates/uart_16550/src/async_/device_ops.rs:128）实现 `embedded_io_async::Write::flush`，调 `tx_completion + register-then-recheck` 走四阶段 drain。

## 实际调用链

```
tcdrain(fd)
  ↓
syscall ioctl(fd, 0x5409, 1)            ← TCSBRK
  ↓
sys_ioctl (kernel/src/syscall/fs/ctl.rs:43-67)
  ↓ 硬编码拦截
调 driver.tx_completion + register-then-recheck
  ↓
block_on → 四阶段 drain
```

不经过：
- ❌ `FileLike::ioctl`
- ❌ `Tty::ioctl`
- ❌ `AsyncUartWriter::flush`

## TCSBRK 硬编码证据

`sys_ioctl` 拦截 TCSBRK（kernel/src/syscall/fs/ctl.rs:43-68）：

```rust
// TCSBRK (0x5409): tcdrain — wait for all TX stages (ring → copier → FIFO → wire)
if cmd == 0x5409 {
    use uart_16550::async_::isr::DRAIN_WAKER;

    use crate::drivers::uart_init;
    let result = block_on(poll_fn(|cx| {
        let driver = uart_init::driver();
        let c = driver.tx_completion();
        if c.is_drained() {
            return Poll::Ready(Ok(0isize));
        }
        // Register waker before recheck (M1 D3 order: register → check)
        if !c.ring_empty || c.copier_active || c.staged_bytes > 0 {
            driver.tx.register_waker(cx.waker());
        }
        DRAIN_WAKER.register(cx.waker());
        let c2 = driver.tx_completion();
        if c2.is_drained() { Poll::Ready(Ok(0isize)) } else { Poll::Pending }
    }));
    result
} else {
    f.ioctl(cmd, arg)...
}
```

`Tty::ioctl`（kernel/src/pseudofs/dev/tty/mod.rs:147-220）处理：TCGETS / TCSETS / TIOCGWINSZ / TIOCSPGRP / TIOCSCTTY / TIOCNOTTY / FIONBIO。**不处理 TCSBRK**。

## driver flush 双份实现反模式

两段代码几乎逐字相同：

`AsyncUartWriter::flush`（device_ops.rs:128-152）：

```rust
let c = self.driver.tx_completion();
if c.is_drained() { return Poll::Ready(Ok(())); }
if !c.ring_empty || c.copier_active || c.staged_bytes > 0 {
    self.driver.tx.register_waker(cx.waker());
}
DRAIN_WAKER.register(cx.waker());
let c2 = self.driver.tx_completion();
if c2.is_drained() { Poll::Ready(Ok(())) } else { Poll::Pending }
```

syscall 层（ctl.rs:48-67）：

```rust
let driver = uart_init::driver();
let c = driver.tx_completion();
if c.is_drained() { return Poll::Ready(Ok(0isize)); }
if !c.ring_empty || c.copier_active || c.staged_bytes > 0 {
    driver.tx.register_waker(cx.waker());
}
DRAIN_WAKER.register(cx.waker());
let c2 = driver.tx_completion();
if c2.is_drained() { Poll::Ready(Ok(0isize)) } else { Poll::Pending }
```

M1 D3 顺序、is_drained 判断、waker 注册全重复。

## 反模式后果

| 后果 | 说明 |
|---|---|
| 违反单一事实来源 | driver 的 `tx_completion` 行为变化时，syscall 不感知 |
| Tty 抽象空洞 | Tty 假装管 tcdrain，实际完全无感 |
| 耦合具体驱动 | `use crate::drivers::uart_init`——非 uart_init 后端拿不到 tcdrain |
| 不可扩展 | PTY、socket、其他外设要 tcdrain 都得在 syscall 复制 |

## 为什么变成这样

Q19B 真板 userbench 需要 tcdrain 通过。最快打通路径：syscall 硬编码。Tty::ioctl 加分支需改抽象、改 PTY、改所有 TTY 后端。当时为赶真板验证选择硬编码。

这是「实施验证快、架构债留痕」的典型。ADR-051 没明确写「tcdrain 走 syscall 硬编码」，是个隐藏决策。

## 正确架构

如果重做：

```
Tty::ioctl(TCSBRK) → 调 AsyncUartWriter::flush
                    ↓
              embedded_io_async::Write::flush
                    ↓
              单一 register-then-recheck 实现
```

收益：
- 任何 TTY 后端（UART、PTY、socket）实现 `embedded_io_async::Write::flush` 即自动支持 tcdrain
- syscall 去掉硬编码，所有 ioctl 走通用路径
- flush 逻辑只有一份

## Q22 维护性清理候选

O48/O49/O50 那组（架构债务清理）有「重复实现去除」一项。这条 TCSBRK 双份实现是典型例子，可列 O50「预留接口评估」或单开 O 条目。

## 易错点

| 误判 | 真相 |
|---|---|
| Tty 抽象处理 tcdrain | Tty::ioctl 不处理 TCSBRK，syscall 层硬编码 |
| tcdrain 走 driver 抽象层 | 走 uart_init 模块，耦合具体驱动 |
| flush 逻辑只一份 | syscall + AsyncUartWriter 各一份 |
| 改 driver 不影响 syscall | 改 `tx_completion` 行为两边都得改 |

## 经验

- flush 不是单一动作，是 5 层不同语义的串接
- Tty 不实现 flush 是有意的设计选择（推给上层 VFS 与底层 driver）
- syscall 硬编码拦截 = 实施快、架构债重
- 单一事实来源被破坏时，「打补丁 + 留 ADR 标注」是必要的善后
- 评估 tcdrain 实现时，先 grep `0x5409` 或 `TCSBRK` 看是不是硬编码

## 深挖补充

**TtyWrite 不实现 flush 的设计意图**

TtyWrite trait 注释（`crates/uart_16550/src/tty.rs:243-260`）明确「non-blocking」。TtyWrite 只承诺「能塞多少塞多少」，不承诺「等发送完成」。

教科书分层：
- TtyWrite 同步非阻塞：当下能写多少
- `embedded_io_async::Write` 异步：何时写完

Tty 抽象留白是有意。flush 由异步层负责。

**为什么不把 flush 加到 TtyWrite**

两个问题：

1. 破坏 TtyWrite 同步非阻塞契约。flush 该返回 `Result<(), _>` 还是 `AsyncResult`？前者要求阻塞。
2. PTY master / slave 需要不同 flush 语义。master 不需要等发送完成；slave 需要。这种差异在 TtyWrite 上不好表达。

`embedded_io_async::Write::flush` 是 async fn，回调能自然适配。当前只有 `AsyncUartWriter` 实现了，PTY 还没接 flush。

**flush 失败如何处理**

UART 错误（overrun、parity、frame）当前被吞：

- `AsyncUartWriter::flush` 返回 `Infallible`（永不错误）
- syscall 层 TCSBRK 返回 `Ok(0isize)`

错误在 ISR 处理，不上报到 flush 路径。教科书设计应报告错误给应用层，当前实施简化。这是 Q22 维护性清理候选。

**教科书设计 vs 实际实现**

教科书「tcdrain 走 TTY 抽象」：

```
用户 tcdrain(fd)
  ↓
syscall → FileLike::ioctl
  ↓
Tty::ioctl(TCSBRK)
  ↓
embedded_io_async::Write::flush
  ↓
register-then-recheck
  ↓
硬件 drain
```

StarryOS 实际「tcdrain 走 syscall 硬编码」：

```
用户 tcdrain(fd)
  ↓
syscall ioctl(0x5409)
  ↓
sys_ioctl 硬编码拦截
  ↓（绕过 Tty 抽象）
调 driver.tx_completion
  ↓
block_on → register-then-recheck
  ↓
硬件 drain
```

差异：教科书 5 层抽象层次清晰。StarryOS 4 层（少 Tty::ioctl 翻译层）+ driver 被硬编码。

触发原因：Q19B 真板 tcdrain 必须通过，最快路径。

**架构债的识别与偿还**

何时偿还 TCSBRK 双份实现？「现在」vs「Q22 维护性清理」：

| 维度 | 现在 | Q22 |
|---|---|---|
| 风险 | 真板验证期间改动可能引入新 bug | 验证已通过，改动风险低 |
| 成本 | 跟当前验证并行 | 单独 milestone |
| 收益 | 立即去除双份实现 | 与其他维护性债务一起处理 |
| 阻塞 | 不阻塞 Q19B | 不阻塞主功能线 |

当前选 Q22 延后是合理工程判断。两个先决条件：

1. ADR 标注清楚——记录 TCSBRK 硬编码是临时方案。当前 ADR-051 没明确写这条，需要补。
2. grep 找得到。`kernel/src/syscall/fs/ctl.rs:43` 注释「TCSBRK (0x5409): tcdrain」是充分提示。

加新 TTY 后端（PTY、socket）时必须重新评估。硬编码拦不住新后端，会暴露「非 UART 不能 tcdrain」的问题。

**什么时候该重写 vs 保留**

「实施快、架构债重」是工程常态。判断何时偿还：

| 信号 | 行动 |
|---|---|
| 同一处债出现 3 次 | 必须重写 |
| 债导致新功能不能加 | 必须重写 |
| 债只在文档里 | 保持 + ADR 标注 |
| 债影响调试效率 | 必须重写 |

TCSBRK 双份实现目前是「债只在文档里」。代码重复但功能正常，调试靠 grep 找得到。Q22 处理合理。

**易错点（笔记之外）**

| 误判 | 真相 |
|---|---|
| 教科书设计 = 正确设计 | 教科书完整但实施成本高 |
| 实施快 = 债 | 工程常态，需 ADR 标注 |
| flush 永远成功 | UART 硬件错误当前被吞 |
| 抽象层加 flush = 好事 | 破坏 TtyWrite 同步非阻塞契约 |
| 5 层都齐 = 完备 | 完备 = 满足需求，不多不少 |

**经验（笔记之外）**

- 教科书设计完整但实施成本高，工程取舍优先
- 「实施快 + ADR 标注 + grep 找得到」是工程常态
- 架构纯洁不是每个项目都负担得起
- TtyWrite 同步非阻塞契约是有意设计，flush 不该破坏
- 加新 TTY 后端是 TCSBRK 双份实现必须重写的临界点