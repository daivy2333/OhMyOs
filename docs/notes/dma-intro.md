# DMA：绕过 CPU 的数据搬运

**日期**：2026-07-21
**标签**：dma, hardware, uart, nic, cache, pio, descriptor, starryos

> 来源：StarryOS UART 驱动实践 + 16550 规范 + 网卡路线分析。
> 范围：DMA 概念、与已有中断驱动 PIO 方案的对比、适用边界。

## 背景：从 PIO 谈起

StarryOS 当前的异步 UART 驱动使用 PIO（Programmed I/O，程控输入输出）。发送一个字节的流程：

```
用户 write() → RingBufTx::push() → copier pop → MMIO wr THR
```

每一步数据搬运都走 CPU。`push` 和 `pop` 等效于 `memcpy`，写 THR 是 `write_volatile` MMIO 操作。所有字节都是 CPU 亲自从一处搬到另一处。

115200 bps（~14.4 KB/s）下这笔开销不大。但如果面对 100 MB/s 的网卡或 500 MB/s 的 NVMe 磁盘，CPU 全部时间都消耗在搬运字节上，无法做其他事。

类比：PIO 是自己一块一块搬砖；DMA 是雇司机开卡车，只告诉他仓库地址、工地地址、多少块砖。

## DMA 是什么

DMA（Direct Memory Access，直接存储器访问）允许外设直接读写系统内存，不需要 CPU 逐字节参与。CPU 只下达命令：源地址、目标地址、长度、方向。剩下的搬运由 DMA 控制器（DMAC）完成。

三个角色：

```
            ┌──────────────────────────────┐
            │          系统总线             │
            │                              │
   ┌────┐  │  ┌──────────┐  ┌─────────┐   │
   │CPU │  │  │   DMAC   │  │  设备   │   │
   │    │  │  │          │  │(UART/NIC│   │
   │    │  │  │          │  │ /Disk)  │   │
   └─┬──┘  │  └────┬─────┘  └────┬────┘   │
     │     │       │              │        │
     │ ①配 DMAC  │              │        │
     │──────────▶│              │        │
     │     │     │  ② 总线读写  │        │
     │     │     │◀────────────▶│        │
     │ ③完成中断 │              │        │
     │◀──────────│              │        │
     │     │     │              │        │
            └──────────────────────────────┘
```

- CPU：配置 DMAC，启动传输，然后被中断通知完成
- DMAC：执行搬运的专用硬件，直接操作总线
- 设备：数据的源或目的地

## DMAC 的硬件角色

DMAC 是总线主设备（Bus Master），能独立发起总线读写事务。普通外设（UART）只是从设备，只能被 CPU 访问。这正是 DMAC 可以绕过 CPU 的根本原因 — 它有操作总线的能力。

DMAC 内部是一个状态机，工作循环：

```
while 还有字节要搬:
  1. 从 src 发起总线读请求 → 得到数据
  2. 向 dst 发起总线写请求 → 写入数据
  3. src += 步长
  4. len -= 1
完成 → 产生中断通知 CPU
```

## PIO vs DMA：4 KB 传输对比

以发送 4 KB 数据到 UART 为例。

PIO 方式：

```
时间 →
CPU: [读buf0][写THR][读buf1][写THR]...[读buf4095][写THR]
     4096 次 load + 4096 次 store = CPU 全程占用
```

DMA 方式：

```
时间 →
CPU: [配置DMAC(~50条指令)]──[去干别的事了]────[处理完成中断(~20条)]
DMAC:                       [读→写]×4096 次总线事务
UART:                       [收字节][发字节][收字节]...
```

CPU 从 ~8192 条指令降到 ~70 条，释放约 99% 的 CPU 时间。

UART 115200 bps 下这个收益不明显（PIO 本身只占约 0.1% CPU），但 1 Gbps 网卡（~125 MB/s）或 NVMe SSD（~3 GB/s）下是无法用 PIO 跟上的 — CPU 指令吞吐根本不够，DMA 是唯一选项。

## 描述符（Descriptor）

单个 buffer 的简单搬运只是入门。实际 DMA 控制器使用描述符环（Descriptor Ring）支持 scatter-gather（分散-聚集）。

一个典型描述符结构：

```
struct DmaDescriptor {
    src:  u32,   // 源物理地址
    dst:  u32,   // 目标物理地址
    len:  u16,   // 本段字节数
    flags: u16,  // OWN 位 + 链式 + 中断使能
    next: u32,   // 下一个描述符地址
}
```

flags 中三个关键位：

- **OWN（所有权位）**：1 = DMAC 拥有此描述符（可读可写），0 = CPU 拥有（DMAC 不碰）。这是 CPU 和 DMAC 之间交接所有权的唯一机制。
- **中断位**：此描述符完成传输后是否产生中断。
- **链式位**：是否跳转到 next 地址继续取下一个描述符。

## 所有权状态机

这和 StarryOS 已有的 `tx_copier_active`（AtomicBool）是同一模式 — 只是消费者从"软件 copier 任务"变成了"DMA 硬件引擎"。

```
软件填描述符 → 设 OWN=1 → 硬件取描述符并搬运 → 完成后设 OWN=0 → 软件回收
     ↑                                                             │
     └─────────────────────────────────────────────────────────────┘
```

描述符环和你已有的 `RingBufTx` 同属环形缓冲区，区别在于：

- `RingBufTx` 里存的是原始字节数据
- DMA descriptor ring 里存的是地址和长度，数据本身在别处
- 所有权交接从 CPU↔CPU（软件任务间）变成 CPU↔DMAC（软件↔硬件）

## 完整传输周期（四阶段）

以 NIC 的 TX descriptor 为例：

**阶段 1：提交**。软件填充 TX descriptor，设 OWN=1，写 doorbell 寄存器通知硬件有新任务。

**阶段 2：DMA 读**。硬件通过总线读 descriptor 内容，验证 OWN=1，读取 payload buffer。

**阶段 3：DMA 传输**。硬件将 payload 写入设备内部 FIFO，设备发送到外部（如网络）。

**阶段 4：完成**。硬件将 descriptor 的 OWN 设回 0，可选地产生中断。软件在 ISR 中扫描 descriptor ring，回收 OWN=0 的项。

## 与 StarryOS UART 组件的对应

| StarryOS UART 组件 | DMA 对应概念 | 相同点 | 不同点 |
|---|---|---|---|
| `RingBufTx`（SPSC ring） | descriptor ring | 环形缓冲区 + 所有权模型 | DMA ring 存地址+长度，不是数据本身 |
| `tx_copier_active`（AtomicBool） | descriptor OWN 位 | "谁在拥有这个资源"的状态 | CPU 原子变量 vs 硬件可见的内存位 |
| copier task（pop→写 THR） | DMAC 引擎（读内存→写设备） | "搬运工消费 ring"的拓扑 | 软件任务轮询 vs 硬件总线事务 |
| `AtomicWaker` + poll 注册 | DMA 完成中断 | "完成后通知上层" | 中断直接触发 waker 唤醒 |
| `TxCompletion::is_drained()` | descriptor 回收确认 | "确认传输完毕"的多阶段检查 | 外加 cache coherence 语义 |
| NAPI 中断合并 | descriptor chain 一次中断覆盖多个描述符 | 批量处理减少中断频率 | 软件实现 vs 硬件原生支持 |

## UART 为什么不一定需要 DMA

UART 115200 bps 下 DMA 的固定开销可能超过 PIO。

- StarryOS D1 真板 PIO 已达物理线速 95.3–99.1%
- DMA 配置一次需要：刷 cache、填描述符、写 doorbell → 几十到上百条指令
- UART TX FIFO 只有 16 字节（`FIFO_SIZE = 16`），DMA 单次传输 ≤ 16 字节
- 16 字节 PIO 搬运只需要 ~32 条指令，DMA 固定开销更大

DMA 的真正价值出现在高速设备上。网卡 125 MB/s、NVMe 3 GB/s — CPU 指令吞吐无法靠 PIO 跟上。

## DMA 引入的三个新问题

PIO 不需要关心的事，DMA 必须处理。

### Cache 一致性

CPU 写 `buffer[0] = 0x41` 可能只在 L1 cache 里，还没到 DRAM。DMAC 读 DRAM 会拿到旧值。

```
CPU:  buffer[0] = 0x41      → 在 L1 cache 中，未写回 DRAM
DMAC: 读 DRAM 地址 buffer[0] → 读到旧值 0x00  ← 错误
```

解决：

- DMA 传输前：**cache clean（flush）**— 把 CPU cache 的脏数据强制写回内存
- DMA 传输后：**cache invalidate** — 丢弃 CPU 旧 cache line，下次访问重新从内存读

PIO 不需要这个是因为 `write_volatile` 到 MMIO 寄存器绕过了 cache。

### 物理地址 vs 虚拟地址

DMAC 工作在物理地址空间。如果 OS 启用了 MMU/页表，CPU 看到的虚拟地址对 DMAC 没有意义。每个描述符中的地址必须做 virt→phys 转换。这就是 DMA 编程中 `DMAInfo { cpu_addr, bus_addr }` 二元组的来源。

### IOMMU 与安全

设备 DMA 不经过 CPU 的 MMU，恶意或出错的设备可能 DMA 写入任意内核内存。IOMMU（I/O 内存管理单元）提供 DMA 侧的地址翻译和权限隔离，限制设备只能访问授权区域。

## 与邻近概念的区分

| 概念 | 一句话 | 与 UART 驱动的关系 |
|---|---|---|
| PIO | CPU 逐指令搬运 | 当前 copier 写 THR 就是 PIO |
| DMA | 专用硬件绕过 CPU 搬运 | 无实现，等待硬件评估 |
| MMIO | CPU 通过内存地址访问设备寄存器 | PIO 和 DMA 配置都用到 MMIO |
| 中断 | 设备通知 CPU 的机制 | PIO 用 THRE 中断，DMA 用传输完成中断 |
| NAPI / 中断合并 | ISR 中批量处理减少中断次数 | 当前 ISR 中连续 pop/push 实现；DMA 用 descriptor chain |

## 代码对照

### 当前 PIO TX copier（简化）

```rust
// CPU 亲自写 MMIO — 这就是 PIO
async fn tx_copier_task(driver: Arc<AsyncUartDriver>) {
    loop {
        wait_for_ring_not_empty().await;
        while let Some(byte) = driver.tx_ring.pop() {
            driver.uart_port.write(THR, byte);
        }
        wait_for_thre_interrupt().await;
    }
}
```

### 假设的 DMA TX manager

```rust
// CPU 不碰数据 — DMAC 做搬运
async fn dma_tx_manager(
    driver: Arc<AsyncDmaUartDriver>,
    dma_ch: DmaChannel,
) {
    let descs: &mut [DmaDescriptor; N] = /* 预分配 */;
    let mut head = 0; // 软件填充位置
    let mut tail = 0; // 硬件完成位置

    loop {
        // 1. 回收已完成描述符（OWN=0）
        while descs[tail].is_completed() {
            descs[tail].mark_free();
            tail = (tail + 1) % N;
        }

        // 2. 填充新描述符
        while head != (tail - 1) % N {
            if let Some((buf_ptr, len)) = get_pending_buffer() {
                cache_clean_range(buf_ptr, len); // ← PIO 不需要这行
                descs[head] = DmaDescriptor {
                    src: phys_addr(buf_ptr),      // ← 物理地址，不是虚拟地址
                    dst: UART_THR_PHYS_ADDR,
                    len, flags: OWN | CHAIN,
                };
                head = (head + 1) % N;
            } else { break; }
        }

        // 3. 通知硬件
        dma_ch.kick(); // 写 doorbell

        // 4. 等待完成中断
        wait_for_dma_completion().await;
    }
}
```

关键变化：

| 变化 | PIO（现状） | DMA（假设） |
|---|---|---|
| 搬运工 | copier task（软件） | DMAC（硬件） |
| ring 里存什么 | 原始字节 | 描述符（地址+长度） |
| CPU 碰数据吗 | 是（pop → write THR） | 否（只管理描述符 ring） |
| memory barrier | Acquire/Release 原子变量 | cache clean/invalidate + fence |
| 地址类型 | 虚拟地址（MMIO 直接可访问） | 必须物理地址 |

## 常见误解

| 误解 | 事实 |
|---|---|
| "DMA 比中断驱动快" | 不一定。DMA 快在吞吐（大数据量），不在延迟。对小数据量，PIO 延迟更低。 |
| "DMA 不需要中断" | 需要。DMA 完成后用中断通知 CPU。只是中断频率降为每次传输一次，而非每字节一次。 |
| "DMA 完全消除 CPU 参与" | 消除的是数据搬运时的 CPU 参与。CPU 仍需配置 DMAC、处理完成中断、管理 descriptor ring。 |
| "有 DMA 就不需要 ring buffer" | 仍需。Descriptor ring 就是 DMA 版 ring buffer，所有权从软件间变成软硬件间。 |

## 动手问题

根据 StarryOS 现有代码，思考：

1. UART FIFO 只有 16 字节。DMA 每次传输 ≤ 16 字节，固定开销（配置 DMAC + 中断处理）可能比 PIO 16 次 MMIO wriite 还大。什么条件下 DMA 才划算？

2. 当前 NAPI 中断合并在 ISR 中连续轮询 FIFO 实现批量搬运。DMA 用 descriptor chain 一次中断覆盖多个描述符。两者本质相同的思想在软件层和硬件层的不同实现。能映射出对应关系吗？

3. 如果 DMAC 配置了 `src=0xDEAD_BEEF`（无效地址），会发生什么？和 PIO copier 访问无效 MMIO 地址的行为有什么不同？
