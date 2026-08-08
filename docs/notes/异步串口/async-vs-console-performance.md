# Async UART 与 polling Console 性能对比

**标签**：uart, async, console, polling, performance, benchmark, qemu, d1, latency, throughput

> 来源：[`docs/benchmark-report-async.md`](https://github.com/Starry-OS/StarryOS/blob/uart-lichee/docs/benchmark-report-async.md)（2026-07-21）、四份原始日志。
> 测试程序 `tests/benchmark.c`，四组环境同版 benchmark、相同 sizes、100 次迭代、相同 drain policy。

## 测试环境

| 环境 | 后端 | 原始日志 |
|---|---|---|
| QEMU rootfs | async UART | [`qemu_out.md`](https://github.com/Starry-OS/StarryOS/blob/uart-lichee/docs/qemu_out.md) |
| D1 真板 command-entry | async UART | [`d1_out.md`](https://github.com/Starry-OS/StarryOS/blob/uart-lichee/docs/d1_out.md) |
| QEMU rootfs | polling Console | [`qemu_console.md`](https://github.com/Starry-OS/StarryOS/blob/uart-lichee/docs/qemu_console.md) |
| D1 真板 command-entry | polling Console | [`d1_console.md`](https://github.com/Starry-OS/StarryOS/blob/uart-lichee/docs/d1_console.md) |

QEMU 不仿真物理线延迟，线速 >100% 是预期现象，只作软件路径和相对行为证据。D1 数据来自物理 UART0（DW APB，115200 bps），是绝对线速依据。

## 结论

D1 上两者同步完成吞吐都接近 115200 bps 上限（96.8%-99.4%），差异 0.6-3.9 个百分点。

**async UART 的区分点在于提交与发送解耦**：D1 上 256B 的 async 入队速度 11,280 KB/s，Console 同步写受限于 11.45 KB/s。用户态 `write()` 将数据推入 ring buffer 即返回，内核 copier + ISR 异步完成物理发送。

**Console 的同步完成延迟更低**：D1 单字节平均 0.106 ms，async 为 0.192 ms（低 44.8%）。async 在 payload >=15B 时每组出现一次 24-27 ms 尾部延迟，Console 无此现象。

## 吞吐

**S10 drain-each**：每次 `write_full()` 后立即 `tcdrain()`。测"用户感知的发送完成"速度。

| Payload | D1 async 线速 | D1 Console 线速 | 差值 |
|---:|---:|---:|---|
| 64B | 96.8% | 99.0% | +2.2 pp |
| 256B | 97.3% | 99.3% | +2.0 pp |
| 1024B | 98.8% | 99.4% | +0.6 pp |

差异随 payload 增大缩小。1024B 时物理发送时间（~88.9 ms）主导端到端延迟。

Console 在 drain-each 上的优势来自路径更短：polling 模式 drain 在 `CONSOLE_LOCK` 内原地等待 TEMT，无 async 的 waker 注册和调度开销。

**S11 提交速度**：S11 语义在两种后端上不同——async 计时窗口只测 ring buffer `push()`（入队），不计发送；Console 每次 write 已同步发送完成。

| Payload | D1 async 入队 | D1 Console 同步写 |
|---:|---:|---|
| 64B | 3,997 KB/s | 11.45 KB/s |
| 256B | 11,280 KB/s | 11.45 KB/s |
| 1024B | 31.79 KB/s | 11.45 KB/s |

64B 和 256B 的 async 入队速度达物理线速的 350-980 倍。1024B 受 64 KiB ring buffer backpressure 限制降到 31.79 KB/s，仍高于 Console。

这是 async UART 的主要架构差异：`write()` 不等待硬件，用户程序可继续执行。

**S12 batch-drain**：连续写 100 次，每 8 次 drain 一次。摊薄 drain 开销后两者差距缩小。

| Payload | D1 async 线速 | D1 Console 线速 |
|---:|---:|---|
| 64B | 98.8% | 99.4% |
| 256B | 98.5% | 99.4% |
| 1024B | 99.1% | 99.4% |

**S13/S14**：`writev()` 4×64B 分片聚合，async 98.7% vs Console 99.3%。小包 128B 时差距最大（95.3% vs 99.2%），差异来自 async 的 copier 调度开销。

## 延迟

**S20 单字节**：写 1B 后立即 drain。测最小粒度下的软件路径开销。

| 指标 | QEMU async | QEMU Console | D1 async | D1 Console |
|---|---|---|---|---|
| avg | 0.176 ms | 0.037 ms | 0.192 ms | 0.106 ms |
| P99 | 0.278 ms | 0.082 ms | 0.238 ms | 0.112 ms |

Console 延迟更低的原因：polling drain 是原地忙等 TEMT；async 经 `write()` → ring push → copier 唤醒 → ISR 发送 → waker 通知 → `tcdrain()` 返回。QEMU 上差距更大（79%），是模拟器 ISR 调度模型所致，不代表真板。

**S21 FIFO 边界**：围绕 16B FIFO 边界（1/15/16/17/31/32/33/48/49B）各 100 次 drain-each。

D1 上的差异：

- Console P99 接近 P50（P99/P50 ≈ 1.00-1.05），无尾部抖动
- async 在 size>=15 时每组出现一次 24-27 ms tail，P99 骤升至 23-27 ms，P99/P50 比 6-19 倍
- async 的 `slow_poll_exh=0`、`yield_exh=0`，fallback 未耗尽，tail 根因不在已知路径

| Size | D1 async P50 | D1 async P99 | D1 Console P50 | D1 Console P99 |
|---:|---:|---:|---:|---:|
| 15B | 1.42 ms | 23.99 ms ⚠️ | 1.30 ms | 1.40 ms |
| 16B | 1.51 ms | 24.68 ms ⚠️ | 1.39 ms | 1.48 ms |
| 32B | 2.87 ms | 25.78 ms ⚠️ | 2.75 ms | 2.85 ms |
| 49B | 4.31 ms | 27.24 ms ⚠️ | 4.20 ms | 4.30 ms |

async P50 与 Console 接近（差值 <5%），典型路径开销相当。tail 疑与 copier 唤醒时序或 ISR 调度竞争有关，未定位根因。

## 稳定性与正确性

| 维度 | async | Console |
|---|---|---|
| TX 完成 | `Done.`，exit 0 | `Done.`，exit 0 |
| drain_errors | 0（全 section） | 0（全 section） |
| short_writes | 0 | 0 |
| S30 RX 非阻塞 | PASS/EAGAIN | UNSUPPORTED |
| S40 telemetry | D1 有效 | 不支持 |

两组后端 TX 路径均具备正确性证据。Console 在 D1 上不支持 RX，无法用于交互式 shell。

## S40 TX 路径行为（仅 async）

D1 async S40 telemetry：

| 指标 | 值 | 含义 |
|---|---|---|
| `user_calls` | 2,577 | 用户态 write 次数 |
| `ring_pop_calls` | 1,659 | copier 从 ring 取数据次数 |
| `hw_send_calls` | 13,842,121 | MMIO THR 写入次数 |
| `hw_send_zero` | 13,820,496 | THR 写 0 字节（FIFO 满）次数 |
| `bytes_per_user_call` | 131.2 | 每次 write 平均字节 |
| `bytes_per_ring_pop` | 203.8 | copier 每次取平均字节 |
| `bytes_per_hw_send` | 0.024 | THR 每次写平均字节（含空写） |

`hw_send_zero` 占 99.85%，是 slow-poll 路径下频繁探测 TX FIFO 状态的正常行为。D1 ISR 为 level-triggered，THRE 位在 FIFO 空时持续为 1，驱动需反复查询。`slow_poll_exh=0` 确认 fallback 未耗尽。

## 取舍

| 维度 | async UART | polling Console |
|---|---|---|
| 提交速度 | 入队即返回 | 受物理线速限制 |
| 同步完成延迟 | 略高（waker/调度开销） | 更低（原地等待） |
| 尾部延迟 | 24-27ms tail（未定位） | 无 tail |
| RX 支持 | 完整 | D1 不支持 |
| 代码复杂度 | ring/copier/ISR/waker/tcdrain | 单线程 polling |
| 并发模型 | ISR + copier task | 单一 polling 上下文 |
| 适用场景 | 交互式 shell、RX/TX 并发 | 纯输出 log |

async UART 的价值不在吞吐（两者都接近物理上限），而在 `write()` 零等待返回、RX/TX 并发、可扩展的 copier 模型。Console 的价值在简单场景的低延迟和零尾部抖动。

个人的理解，受限于真板波特率和串口本身的特性，异步化并没有带来很多性能上的提升，本身任务比较简单，异步化的那些结构成本、通知开销反而是成为了问题的一部分，在训练营期间测试得到的那些数据很有可能是当时测试代码设计不好导致的优化幻觉，但是话又说回来，异步化就没有好处么，其实是有的，在性能报告里面有相当多的数据说明，假如有更高的波特率、大包的数据搬运任务、更多的hart，异步串口的优势就越明显，而且异步化本身能够减少轮询空转造成的阻塞和cpu占用，只是遗憾starryos不能够支撑进行cpu占用的测试，不能拿到实际数据做支撑。

现在话就可以这样说了，异步串口在一点点常规任务性能下降（2%）和内存占用（128kb）的情况下享受了异步架构带来的诸多好处，这不能不说是一种优势，或许等另一个多hart开发板到手了，console的阻塞设计就会暴露真正的问题，异步架构的优势才能完全显现。

那么我不妨再提出一个观点，当前只是测试搬运能力，在搬运字节能力之外，异步的提交就返回的优势可以让程序去做别的事情而不是阻塞，这或许是console做不到的事情。

结论就是console适合单核串行场景，这种简单的任务，console还有0内存占用和实现简单的优势，异步uart的话更适配多hart并行场景。

另外qemu和荔枝派各自的硬件策略不太一样，导致适配层如果没做好就容易出现各种问题，比如上面qemu的更大延迟和d1的p99长尾，架构复杂或许会成为异步uart的问题之一。

---

Q31 和 Q32 补了 CPU 效率（S41 单字节 CPU 开销）、计算重叠（S42 写后空窗）和定时器唤醒抖动（S43）三个维度之后，之前很多靠推测的东西现在有数字了。

S41 的数据说实话比我想象的更夸张。D1 上 Console 发 1 字节只花 1100 条指令，async 要 32000-44000 条——差了 30 到 40 倍。这倒不是说 async 写路径写得烂，而是 polling 模式下路径短到离谱：`write()` → 查 THRE → 写 THR → 等 TEMT → 返回，没有 ring push、没有 copier 调度、没有 ISR 唤醒、没有 waker 注册。每一条 async 的"架构优势"在这条短路上都变成了一份指令开支。只是我们没办法测 CPU 占用率，`instret` 是 hart 全量的指令计数器代理，不是百分比，不能算进 utilization。

但 S42 直接把之前"提交就返回可以做别的事"的推测变成了实测证据。D1 async 写 100 次 64B 入队只花 1.6 ms 就返回了，剩下 550 ms 的 UART 发送时间里 CPU 跑计算内核拿到了 53.5% 的 overlap——也就是说 `write()` 返回后的空窗里，CPU 还有一半多的时间能干正事。Console 同步写这边呢？554 ms 全在 `write()` 调用窗口里烧完了，overlap 是 0。不是 bug，是 polling 模式下物理必然——UART 没发完你就不可能返回。所以之前说的"异步让程序不阻塞"这东西，在 D1 上有了具体数字：一半多的时间是可用的。

S43 也很有意思。idle 纯睡眠两组差不多（Console 8.4ms vs async 9.5ms P50），没什么区别。loaded 场景下 Console 直接 `not-applicable`——同步写把理论线时窗口全耗光了，根本没剩时间去观测 loaded 的唤醒偏移。async loaded P50 是 25.78ms，比 idle 高了不少，但这延迟是发送积压期间的唤醒干扰，不是空闲时的问题。这里暴露了一个 Console 在 loaded 场景下的结构性缺陷：它不是"测不到 loaded"，而是"loaded 场景本身就不存在"，因为没有 overlap 窗口。换句话说，只要开始发送，Console 就被锁死了。

现在回看我之前写的结论，基本还在线上，但有几点要修正。之前说"受限于真板波特率和串口本身的特性，异步化并没有带来很多性能上的提升"，这句话现在看不够准确——换一种说法就是"同步完成吞吐确实没拉开（差异在 2% 以内），但不是因为异步化没用，而是吞吐维度天花板太低，两者都在撞墙"。S41/S42/S43 这几个非吞吐维度的数据反而说明，真正的分化在吞吐之外：每字节的 CPU 开销 async 高了 30 倍（劣势），但 `write()` 返回后的空闲窗口拿了 53.5%（优势）。劣势在 instructions count，优势在 concurrency window。

之前写的"等待另一个多 hart 开发板到手，console 的阻塞设计就会暴露真正的问题"这个推测也被 S42 间接印证了——单 hart 下 Console 的 overlap 是 0，多 hart 下一个 hart 阻塞 UART 发送时其他 hart 必须等，async 不存在这个问题，copier task 可以把脏活全接了。只是我们还是没拿到多 hart 的实测数据。

另外 S41 暴露了一个 async 不能回避的问题：即使用了 batch-drain（S12 把差距压到 0.3-0.9 pp），单字节开销还是差了 30 倍。这说明 async 在纯吞吐类的同步完成路径上没有跟 Console 竞争的资本——如果需求就是"尽快发完然后 wait"，那 polling 就是更好的选择。async 好在对"发出去但不急着等"这类场景——比如 kernel log、大量后台数据传输、需要 RX/TX 同时工作的交互 shell。