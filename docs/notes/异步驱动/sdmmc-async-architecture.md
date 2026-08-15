# SDMMC 驱动异步架构分析（simple-sdmmc-extended）

**日期**：2026-08-14
**标签**：rust, async, driver, sdmmc, dma, idmac, arceos, visionfive2

> 来源：[xianxw/Final-NO-SDMMC](https://github.com/xianxw/Final-NO-SDMMC) 仓库 `modules/simple-sdmmc-extended/src/sdmmc.rs`（revision [`f0bdece`](https://github.com/xianxw/Final-NO-SDMMC/tree/f0bdecedf50047a4efee598ee39080e109f2f25e)），以及 `dma.rs`、`cmd.rs`、`axtask` 的 future/wait_queue 实现。
> 分析对象是 xianxw 在 fork 的 SD/MMC 驱动中构建的异步接口层，重点是它做了什么、等待机制是否真异步、上层是否消费。

## 结论

simple-sdmmc-extended 是**同步为主 + 异步 API 就绪**的双路径架构，不是端到端异步架构。

- 驱动内实现了完整的异步块读写 API（`read_blocks_async` / `write_blocks_async` 等），依赖 IRQ 驱动的完成通知，等待是真异步（事件 + 定时器轮），不是忙等伪装。
- 上层块设备栈（`axdriver_block-m` 的 `BlockDriverOps`）只暴露同步接口，异步 API 在仓库内无调用者。运行时实际走同步路径。
- 异步 future 在 DMA 提交后被 drop 会永久 fault 驱动（fail-stop 策略），取消不可恢复。

## 驱动做了什么

xianxw 在 `modules/simple-sdmmc-extended`（fork 自 simple-sdmmc）中实现了 IDMAC DMA 块传输，并在此基础上加了一层异步 API。同一份传输逻辑提供同步和异步两个入口：

| 异步 API | 对应同步 API | 作用 |
|---|---|---|
| [`read_blocks_async`](https://github.com/xianxw/Final-NO-SDMMC/blob/f0bdecedf50047a4efee598ee39080e109f2f25e/modules/simple-sdmmc-extended/src/sdmmc.rs#L1290) / [`write_blocks_async`](https://github.com/xianxw/Final-NO-SDMMC/blob/f0bdecedf50047a4efee598ee39080e109f2f25e/modules/simple-sdmmc-extended/src/sdmmc.rs#L1334) | [`read_blocks`](https://github.com/xianxw/Final-NO-SDMMC/blob/f0bdecedf50047a4efee598ee39080e109f2f25e/modules/simple-sdmmc-extended/src/sdmmc.rs#L1263) / [`write_blocks`](https://github.com/xianxw/Final-NO-SDMMC/blob/f0bdecedf50047a4efee598ee39080e109f2f25e/modules/simple-sdmmc-extended/src/sdmmc.rs#L1312) | 公开块读写，按 chunk 循环 await |
| [`read_dma_chunk_async`](https://github.com/xianxw/Final-NO-SDMMC/blob/f0bdecedf50047a4efee598ee39080e109f2f25e/modules/simple-sdmmc-extended/src/sdmmc.rs#L1078) / [`write_dma_chunk_async`](https://github.com/xianxw/Final-NO-SDMMC/blob/f0bdecedf50047a4efee598ee39080e109f2f25e/modules/simple-sdmmc-extended/src/sdmmc.rs#L1240) | [`read_dma_chunk`](https://github.com/xianxw/Final-NO-SDMMC/blob/f0bdecedf50047a4efee598ee39080e109f2f25e/modules/simple-sdmmc-extended/src/sdmmc.rs#L1052) / [`write_dma_chunk`](https://github.com/xianxw/Final-NO-SDMMC/blob/f0bdecedf50047a4efee598ee39080e109f2f25e/modules/simple-sdmmc-extended/src/sdmmc.rs#L1218) | 单 chunk 的 DMA 传输 |
| [`send_cmd_idmac_async`](https://github.com/xianxw/Final-NO-SDMMC/blob/f0bdecedf50047a4efee598ee39080e109f2f25e/modules/simple-sdmmc-extended/src/sdmmc.rs#L2005) | [`send_cmd_idmac`](https://github.com/xianxw/Final-NO-SDMMC/blob/f0bdecedf50047a4efee598ee39080e109f2f25e/modules/simple-sdmmc-extended/src/sdmmc.rs#L2056) | 提交命令 + 等待 DMA 完成 |
| [`wait_transfer_async`](https://github.com/xianxw/Final-NO-SDMMC/blob/f0bdecedf50047a4efee598ee39080e109f2f25e/modules/simple-sdmmc-extended/src/sdmmc.rs#L1971) | [`wait_transfer_sync`](https://github.com/xianxw/Final-NO-SDMMC/blob/f0bdecedf50047a4efee598ee39080e109f2f25e/modules/simple-sdmmc-extended/src/sdmmc.rs#L1939) | 等待命令完成 / DMA 终态，超时 2s / 5s |
| [`wait_card_ready_after_write_async`](https://github.com/xianxw/Final-NO-SDMMC/blob/f0bdecedf50047a4efee598ee39080e109f2f25e/modules/simple-sdmmc-extended/src/sdmmc.rs#L1116) | 同步 busy 轮询 | 写后 CMD13 轮询卡片编程状态 |

配套结构：

- [`ActiveIdmacTransfer`](https://github.com/xianxw/Final-NO-SDMMC/blob/f0bdecedf50047a4efee598ee39080e109f2f25e/modules/simple-sdmmc-extended/src/sdmmc.rs#L239)：传输的 RAII guard，提供 `wait_async` / `wait_sync` 双路径；`Drop` 时若 DMA 已提交则停止 IDMAC 并 fault 驱动
- [`AsyncWriteBusyGuard`](https://github.com/xianxw/Final-NO-SDMMC/blob/f0bdecedf50047a4efee598ee39080e109f2f25e/modules/simple-sdmmc-extended/src/sdmmc.rs#L311)：写 busy 未决时 `Drop` 即 fault 驱动
- [`IdmacCompletion`](https://github.com/xianxw/Final-NO-SDMMC/blob/f0bdecedf50047a4efee598ee39080e109f2f25e/modules/simple-sdmmc-extended/src/sdmmc.rs#L53)：代际计数 + 原子位快照，IRQ 里记录 RINTSTS/IDSTS，任务按 generation 读取，解决"IRQ 先于等待者注册"的竞态
- [`cooperative_yield_once`](https://github.com/xianxw/Final-NO-SDMMC/blob/f0bdecedf50047a4efee598ee39080e109f2f25e/modules/simple-sdmmc-extended/src/sdmmc.rs#L119)：`poll_fn` 立即自唤醒一次，协作式让出 CPU

## 异步数据流（读路径）

```
read_blocks_async(block, buf)
  └─ chunk 循环
     └─ read_dma_chunk_async
        ├─ set_transaction_size(blksiz, bytcnt)
        ├─ send_cmd_idmac_async
        │  ├─ prepare_idmac_transfer   // 轮询等 idle、清 W1C 状态、分配并发布描述符
        │  ├─ start_idmac_transfer     // begin_transfer() 取 generation，写 CMDARG/CMD
        │  ├─ ActiveIdmacTransfer::new
        │  └─ wait_transfer_async().await
        │     ├─ [response_expect] wait_timeout_until_async(2s, command_done_or_error)
        │     ├─ 检查 error
        │     └─ wait_timeout_until_async(5s, terminal_events_or_error)
        │        // 挂起：listener.await，等待 IRQ 或定时器
        ├─ validate_idmac_terminal()   // 校验 RINTSTS/IDSTS/描述符 OWN/CES
        ├─ validated_response()        // 校验 R1
        └─ finish_idmac_transfer(false) // 清状态、释放描述符
  └─ buf.copy_from_slice(dma_buf)     // 从 DMA 缓冲拷回用户缓冲
```

## 中断唤醒边

```
硬件 DMA 完成 / 错误
  └─ IRQ → SdMmcDriver::irq_handler() → SdMmc::dma_irq_handler()
     ├─ 读 RINTSTS / IDSTS
     ├─ has_idsts → regs.idsts().write(idsts)      // W1C
     ├─ transfer_event || error → 构造并写回 RINTSTS 子集 // W1C，避免丢 command_done 语义
     ├─ IDMAC_COMPLETION.record_irq(rintsts, idsts) // 按当前 generation 记录快照
     ├─ error → IDMAC_ERROR_FLAG.store(true)
     └─ should_notify → IDMAC_DONE_FLAG.store(true) + IDMAC_WAIT_QUEUE.notify_one(false)
        └─ 唤醒 wait_transfer_async → 重查 condition → Ready
```

入口在 [`dma_irq_handler`](https://github.com/xianxw/Final-NO-SDMMC/blob/f0bdecedf50047a4efee598ee39080e109f2f25e/modules/simple-sdmmc-extended/src/sdmmc.rs#L2099)，由 `axdriver_block-m` 的 [`SdMmcDriver::irq_handler`](https://github.com/xianxw/Final-NO-SDMMC/blob/f0bdecedf50047a4efee598ee39080e109f2f25e/modules/axdriver_block-m/src/sdmmc.rs#L27) 转发。

同步路径的 `wait_transfer_sync` 不进事件队列，靠自旋读寄存器判断同一组 `idmac_*_or_error` 条件，与异步路径共享 `IdmacCompletion` 快照。

## 等待机制：真异步还是忙等

数据阶段是真异步：

- [`wait_transfer_async`](https://github.com/xianxw/Final-NO-SDMMC/blob/f0bdecedf50047a4efee598ee39080e109f2f25e/modules/simple-sdmmc-extended/src/sdmmc.rs#L1971) 调用 `IDMAC_WAIT_QUEUE.wait_timeout_until_async()`，这是 [`axtask::WaitQueue`](https://github.com/xianxw/Final-NO-SDMMC/blob/f0bdecedf50047a4efee598ee39080e109f2f25e/modules/axtask/src/wait_queue.rs#L118) 的异步方法，底层用 `event_listener::Event` 挂起 + `timeout_at` 定时器轮超时
- 挂起期间任务不占 CPU，由 IRQ 的 `notify_one(false)` 或定时器唤醒
- 写 busy（CMD13）等待用 `cooperative_yield_once()` 协作让出，不忙等

命令提交和响应等待（`send_cmd_with_deadline`、`wait_transfer_sync` 前半段）仍是自旋轮询。所以异步化覆盖的是 DMA 完成和卡片 busy 两个耗时阶段，命令阶段保留轮询。

## 上层接线状态

`axdriver_block-m/src/sdmmc.rs` 的 [`SdMmcDriver`](https://github.com/xianxw/Final-NO-SDMMC/blob/f0bdecedf50047a4efee598ee39080e109f2f25e/modules/axdriver_block-m/src/sdmmc.rs#L10) 实现同步 `BlockDriverOps`（`read_block` / `write_block` → `read_blocks` / `write_blocks`），`irq_handler` 转发到 `dma_irq_handler`。仓库内 `read_blocks_async` 等异步 API 无调用者，异步层未接线到文件系统或 syscall 层。

## 取消语义：fail-stop

DMA 提交后 drop 异步 future 会触发 [`ActiveIdmacTransfer::drop`](https://github.com/xianxw/Final-NO-SDMMC/blob/f0bdecedf50047a4efee598ee39080e109f2f25e/modules/simple-sdmmc-extended/src/sdmmc.rs#L295)：停止 IDMAC、清中断、释放描述符，并置 `idmac_faulted = true`。之后所有传输返回 `DriverFaulted`。这是刻意的 fail-stop——防止 DMA 访问已释放的描述符（use-after-free），代价是取消不可恢复。写 busy 未决时 drop 同理，由 [`AsyncWriteBusyGuard`](https://github.com/xianxw/Final-NO-SDMMC/blob/f0bdecedf50047a4efee598ee39080e109f2f25e/modules/simple-sdmmc-extended/src/sdmmc.rs#L311) fault 驱动。

## 超时层次

| 阶段 | 超时 | 说明 |
|---|---|---|
| 命令响应 | 2s（`IDMAC_COMMAND_TIMEOUT`） | 正常由 IRQ 唤醒 |
| 数据阶段兜底 | 5s（`IDMAC_DATA_WATCHDOG_TIMEOUT`） | 控制器 TMOUT 错误会先经 IRQ 唤醒 |
| 写 busy | 5s（`ASYNC_WRITE_BUSY_TIMEOUT`） | CMD13 轮询 |
| START_CMD 接受 | 1ms（`START_CMD_SPIN_TIMEOUT`） | 自旋 |
| 提交前 idle | 100us（`PRE_SUBMIT_IDLE_SPIN_TIMEOUT`） | 自旋 |

错误映射：`SdMmcError` 22 个变体，上层 `map_error` 转成 `DevError`。

## 未确认项

- 异步接口无调用者，是否计划接入异步块层（io_uring 风格或异步 VFS），仓库内无证据
- 异步路径（尤其挂起/唤醒时序）在真板上是否经过压力验证，仓库内无测试产物；现有 SDMMC 记录文档对应的是同步中断循环修复
- `IDMAC_WAIT_QUEUE` 是 `static` 全局单例，`notify_one` 只唤醒一个等待者；多实例或多并发 DMA 传输场景未处理

## 参考

- [异步 UART 驱动总体架构](../异步串口/async-uart-driver-architecture.md)：同一 ArceOS 平台的异步驱动参考
- [异步网卡架构探索](../异步网卡/async-nic-architecture-exploration.md)：descriptor 级异步驱动的分层参考
