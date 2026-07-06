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