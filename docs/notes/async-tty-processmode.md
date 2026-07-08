# TTY 阻塞读与 ProcessMode 桥接

**日期**：2026-07-04
**标签**：rust, tty, ldisc, async, waker

> 来源：StarryOS `kernel/src/pseudofs/dev/tty/terminal/ldisc.rs:31-50`、`ldisc.rs:219-223`、`ldisc.rs:271-294`、`ldisc.rs:200-216`、`kernel/src/pseudofs/dev/tty/mod.rs:57`、`mod.rs:244-261`、`kernel/src/pseudofs/dev/tty/pty.rs:58-86`、`kernel/src/drivers/ntty_async.rs:25-31`。
> 范围：TTY 阻塞读在哪个 waker 上等、ProcessMode 三态选择、tty-reader 后台 task、PTY 主从与 AsyncUart 的实际用法。

## 答案

ProcessMode 解决「TTY 阻塞读该在哪个 waker 上阻塞」的选择问题。三态对应不同数据来源。

## TTY 阻塞读的根问题

用户态调 `read(stdin)`，数据没来时进程要让出 CPU。问题：数据来源是哪里？

| TTY 角色 | 数据来源 | 阻塞 waker 应注册到 |
|---|---|---|
| 真实 UART | UART RX 中断 | UART PollSet |
| PTY master | 不应有 rx | 不需要 |
| PTY slave | 来自 master 写入 | master 端 PollSet |
| 控制台 | 同真实 UART | UART PollSet |

## 三个变体

定义（`ldisc.rs:31-50`）：

```rust
pub enum ProcessMode {
    Manual,                                     // 仅 read 时处理
    External(Box<dyn Fn(Waker) + Send + Sync>), // spawn task，外部 callback 注册 waker
    None(Arc<PollSet>),                          // PTY master 用
}
```

内部 `Processor`（`ldisc.rs:219-223`）：

```rust
enum Processor<R, W> {
    Manual(InputReader<R, W>),
    External(Arc<PollSet>),
    None(SimpleReader<R>, Arc<PollSet>),
}
```

## 三个变体的差异

### Manual

`Processor::Manual(InputReader)`。`InputReader::poll()` 走完整 line discipline（IGNCR/ICRNL/ICANON/ECHO）。

`register_rx_waker` 直接 `waker.wake_by_ref()`（`ldisc.rs:345-347`）——不真阻塞。

限制：信号只能 read 时检查。`ldisc.rs:33-37` 注释明确指出。

### External

`Processor::External(Arc<PollSet>)`。spawn 独立 task（`ldisc.rs:271-294`）：

```rust
ProcessMode::External(register) => {
    let poll_rx = Arc::new(PollSet::new());
    axtask::spawn_with_name(
        {
            let poll_rx = poll_rx.clone();
            let poll_tx = poll_tx.clone();
            move || {
                block_on(poll_fn(|cx| {
                    while reader.poll() { poll_rx.wake(); }
                    poll_tx.register(cx.waker());
                    register(cx.waker().clone());
                    while reader.poll() { poll_rx.wake(); }
                    Poll::Pending
                }))
            }
        },
        "tty-reader".into(),
    );
    Processor::External(poll_rx)
}
```

机制：
- spawn 一个 tty-reader 后台 task
- 持续 `reader.poll()`，走 InputReader 完整 line discipline
- 数据推 buf_tx 后 `poll_rx.wake()` 唤醒消费者
- 没数据时调外部 `register(cx.waker().clone())`
- 把 waker 挂到数据源 PollSet

关键：外部 callback 是「数据源 wake 我」的钩子。

### None

`Processor::None(SimpleReader, Arc<PollSet>)`。`SimpleReader::poll()` 只做 LF→CRLF 转换（`ldisc.rs:200-216`），不走 line discipline。

用途：PTY master。master 是 raw 通道，不需要完整行规程。

## PTY 主从的选型

`pty.rs:58-86`：

```rust
let master = Tty::new(
    terminal.clone(),
    TtyConfig {
        reader: PtyReader::new(slave_to_master.clone()),
        writer: PtyWriter::new(master_to_slave.clone(), poll_rx_slave.clone()),
        process_mode: ProcessMode::None(poll_rx_master.clone()),
    },
);

let slave = Tty::new(
    terminal,
    TtyConfig {
        reader: PtyReader::new(master_to_slave),
        writer: PtyWriter::new(slave_to_master, poll_rx_master),
        process_mode: ProcessMode::External(Box::new(move |waker| {
            poll_rx_slave.register(&waker)
        })),
    },
);
```

slave 端 External callback 把 waker 注册到 `poll_rx_slave`——master 端 PollSet。master 的 `PtyWriter.write` 写完数据后 `self.1.wake()` 唤醒 `poll_rx_slave`，触发 tty-reader。

数据流：

```
键盘输入
  ↓
master PtyWriter.write
  ↓ push master_to_slave，wake poll_rx_slave
slave 端 tty-reader 被唤醒
  ↓ InputReader.poll() 走 line discipline
  ↓ 数据推 buf_tx
  ↓ poll_rx.wake()
slave 端 read 消费者被唤醒
  ↓
返回数据
```

`is_ptm = matches!(&config.process_mode, ProcessMode::None(_))` 在 Tty 层面区分 master / slave：

- `is_ptm` 时跳过 job control（`mod.rs:254`）
- `is_ptm` 时无条件轮询 IN（`mod.rs:247`）
- `is_ptm` 时跳过前台检查（`mod.rs:98`）

master 不是真 TTY，是 raw 通道，不该受 job control 限制。

## AsyncUart 的 ProcessMode

`ntty_async.rs:25-31`：

```rust
process_mode: ProcessMode::External(Box::new(move |waker| {
    // register the tty-reader's waker on the RX ring buffer's PollSet.
    uart_init::driver().rx.poll.register(&waker);
})),
```

callback 把 tty-reader waker 注册到 UART RX ring buffer 的 PollSet。

数据流：

```
UART RX 收到字节
  ↓
RX copier push_batch 到 ring buffer
  ↓ rx.poll.wake() (ring_buffer.rs:83/101)
tty-reader task 被唤醒
  ↓
InputReader.poll() 走 line discipline
  ↓ poll_rx.wake()
shell 端 read() 拿到数据
```

## 设计动机

`ldisc.rs:33-37` 注释明确指出：

> Manual: Process inputs only on call to `read`. This is the fallback strategy and is rather limited. For instance, you can't interrupt a running program by Ctrl+C unless it's not blocked on a `read` call to the terminal, since the signal is emitted only when inputs are being processed.

Manual 是 fallback。External 模式的设计动机就是解决「信号延迟」问题：通过 spawn 后台 task 持续 poll，让信号能在任何时候被处理。

None 是另一个动机：master 不是真 TTY，是数据通道，不需要完整行规程。

## 易错点

| 误判 | 真相 |
|---|---|
| Manual 阻塞 = 没用 | 适合简单场景（不需后台处理）|
| External = 数据自动来 | callback 只是 wake 路径，数据要 tty-reader 主动 poll |
| None = 简化版 Manual | PTY master 专用，走 SimpleReader 不走完整 ldisc |
| 选 ProcessMode 决定 PollSet 在哪 | ProcessMode + callback 一起决定，缺一不可 |
| 三态只是性能差异 | 设计动机不同：Manual=简单、External=可被外部 wake、None=master 专用 |

## 经验

- TTY 异步化的关键是「在哪阻塞」：数据来源决定 PollSet 位置
- 选 ProcessMode 前先问「数据从哪来」
- Manual 模式是 fallback，不是首选
- External 的 callback 是数据源 wake 钩子，不是数据本身
- PTY master 不是真 TTY，强制走 None 走 SimpleReader

## 问题解答

### 问题 1：PTY master 错选 `ProcessMode::External` 会怎样？

会引发三个问题：

1. **line discipline 双重应用**。External 模式 spawn 的 tty-reader task 用 `InputReader`（走完整 ldisc），PTY master 路径不该有 ldisc。从 slave 来的数据已经过 slave 端 ldisc，到 master 又被 InputReader 处理一遍——`\r`→`\n` 重复、信号重复分发。

2. **信号错乱**。InputReader 的 `check_send_signal` 会扫描 `\x03`（SIGINT）、`\x1c`（SIGQUIT）等控制字符。master 端不该触发这些——它们是给应用层用户的，不是给 PTY master 的。

3. **`is_ptm` 状态错位**。`is_ptm = matches!(&config.process_mode, ProcessMode::None(_))` 会变 false。master 端原本跳过 job control（`mod.rs:254`）、跳过前台检查（`mod.rs:98`）的优势会消失。master 被误当作普通 TTY 处理，PTY 客户端发数据时可能受前台进程组检查阻塞。

**根本原因**：master 是「写者视角」的 raw 通道，不是 TTY 用户视角。`None` + `SimpleReader` 的设计正是为了避免这些问题。

### 问题 2：Manual 模式下后台进程按 Ctrl+C 会怎样？

**会失败**——Manual 模式不支持后台进程的 Ctrl+C 中断。

具体路径：

- Manual 的 `register_rx_waker` 直接 `waker.wake_by_ref()`（`ldisc.rs:346`），不真阻塞
- 没有外部 callback 注册到 RX PollSet
- ISR 收到 `\x03` → push 到 RX ring buffer
- 没有 waker 被注册到 RX PollSet → 没人被唤醒
- 后台进程不在 read → 不会被 ldisc 处理
- `\x03` 字节躺在 ring buffer 里等到下次 read
- 如果后台进程永远不 read，信号永远不到

这正是 `ldisc.rs:33-37` 注释说的「can't interrupt a running program by Ctrl+C unless it's not blocked on a `read` call to the terminal」。

**External 模式如何解决**：spawn 一个独立 tty-reader task 持续 poll。即使没人 read，task 也在后台处理 RX 字节、扫描控制字符、分发信号。后台进程能即时收到 SIGINT。