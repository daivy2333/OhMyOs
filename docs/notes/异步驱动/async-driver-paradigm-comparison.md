# 两种异步驱动范式对比：UART copier 流式 vs SDMMC 请求-响应

**日期**：2026-08-14
**标签**：rust, async, driver, uart, sdmmc, dma, atomic-waker, wait-queue, architecture

> 对比对象：
> - 我的 UART：[daivy2333/StarryOS](https://github.com/daivy2333/StarryOS) `crates/uart_16550/src/async_/`（revision [`5d1a2268`](https://github.com/daivy2333/StarryOS/tree/5d1a22689ed37d657c0ae39251a2e01980b50ec3)）
> - xianxw 的 SDMMC：[xianxw/Final-NO-SDMMC](https://github.com/xianxw/Final-NO-SDMMC) `modules/simple-sdmmc-extended/src/sdmmc.rs`（revision [`f0bdece`](https://github.com/xianxw/Final-NO-SDMMC/tree/f0bdecedf50047a4efee598ee39080e109f2f25e)）
> 两个驱动都叫异步驱动，但属于两种不同的异步范式，差异源于设备语义：UART 是字节流设备，SDMMC 是块设备。

## 核心差异

我的 UART 是 **copier 任务 + ring buffer + AtomicWaker** 的流式异步；xianxw 的 SDMMC 是 **请求-响应 + 条件等待 + 全局队列** 的事务式异步。

```
UART：   生产者/消费者 —— 数据持续流经 ring buffer，copier 常驻搬运
SDMMC：  请求/响应 —— 一次 DMA 传输 = 一次命令提交 + 一次完成等待
```

## 六维度对比

### 1. 等待原语

| | UART | SDMMC |
|---|---|---|
| 原语 | `embassy_sync::AtomicWaker` | `axtask::WaitQueue`（`event_listener::Event`） |
| 等待方式 | `waker.register(cx.waker())` 存单槽，ISR `wake()` | `listener.await` 挂起任务，ISR `notify_one()` |
| 唤醒后 | 任务直接重新调度继续跑 | 重新 poll 并重查条件，不满足继续挂 |

差异本质：UART 的 waker 唤醒对应"硬件 FIFO 有数据/空了"这个已确认事件；SDMMC 的 DMA 完成状态在寄存器里，notify 只是"提示去读"，读寄存器确认后才算完成，所以必须条件重查（`idmac_terminal_events_or_error`）。

### 2. 数据流架构

| | UART | SDMMC |
|---|---|---|
| 搬运者 | RX/TX copier 常驻后台任务 | 无 copier，调用方自己 await |
| 缓冲 | SPSC ring buffer（64KB×2），embassy_hal_internal | DMA 直连调用方缓冲区（IDMAC 描述符链），零中间拷贝 |
| 生命周期 | 任务与驱动同生命周期 | 每次传输创建 `ActiveIdmacTransfer` RAII guard |

ring buffer 解决"字节流速率不匹配"，DMA 解决"拷贝成本"，两者解决的问题不同。SDMMC 块传输粒度确定（512B 整数倍、有界），不需要中间缓冲。

### 3. ISR 职责

| 步骤 | UART（[isr.rs L83](https://github.com/daivy2333/StarryOS/blob/5d1a22689ed37d657c0ae39251a2e01980b50ec3/crates/uart_16550/src/async_/isr.rs#L83)） | SDMMC（[sdmmc.rs L2099](https://github.com/xianxw/Final-NO-SDMMC/blob/f0bdecedf50047a4efee598ee39080e109f2f25e/modules/simple-sdmmc-extended/src/sdmmc.rs#L2099)） |
|---|---|---|
| 1 | 读 ISR 判断中断类型 | 读 RINTSTS + IDSTS |
| 2 | **禁用对应 IER 位**（mask） | 清 IDSTS（W1C 全清） |
| 3 | wake 对应 waker（RX/TX/DRAIN） | **选择性清 RINTSTS**（只清事件位，保留 command_done） |
| 4 | 返回 | `record_irq` 存代际快照 + `notify_one` |

UART 的 mask 机制天然免疫"中断源未清导致的无限重触发"——16650 只要 disable IER 就不会再触发，不需要清状态位。SDMMC 的 INTMASK 在 `try_enable_idmac` 一次配好后不动，防重触发只能靠 W1C 清干净中断源，清不干净就无限循环（见仓库内 `SDMMC_IRQ_HANG_ANALYSIS.md`）。

### 4. 取消语义

| | UART | SDMMC |
|---|---|---|
| drop future 后果 | 无特殊处理，copier 常驻 | **永久 fault 驱动**（`idmac_faulted = true`） |
| 恢复 | 天然可恢复 | 不可恢复，后续传输全返回 `DriverFaulted` |

UART 的取消边界在上层（`AsyncUartReader`/`AsyncUartWriter` 的 drop 只放弃 ring 访问权，不影响 copier 和硬件状态机），取消风险没有下沉到驱动层。SDMMC 每个传输是独立 future，drop 时 DMA 可能还在飞，释放描述符就是 use-after-free，fail-stop 是"宁可杀死驱动不让 DMA 碰已释放内存"的取舍。

### 5. 竞态防护（防丢唤醒）

| 竞态 | UART | SDMMC |
|---|---|---|
| 事件先于注册（lost wakeup） | **register-recheck**：注册后重查硬件（如 [tx_copier_loop L566-570](https://github.com/daivy2333/StarryOS/blob/5d1a22689ed37d657c0ae39251a2e01980b50ec3/crates/uart_16550/src/async_/driver.rs#L566) 注册后重查 `transmitter_empty`） | **代际快照**：IRQ 把状态 OR 进 `IdmacCompletion` 快照，等待方按 generation 读 |

register-recheck 是时间上闭合（注册后立刻重查，已发生就当作已唤醒）；代际快照是状态上闭合（中断状态被持久化，晚来也能读到）。后者更彻底但多了 generation 匹配逻辑（快照可能属于上一次传输，需对号）。

### 6. 可移植性与依赖

| | UART | SDMMC |
|---|---|---|
| OS 抽象 | 3 个 trait：`OsRuntime`（[os/mod.rs L21](https://github.com/daivy2333/StarryOS/blob/5d1a22689ed37d657c0ae39251a2e01980b50ec3/crates/uart_16550/src/os/mod.rs#L21)）、`OsWakerSet`（L49）、`UartPort`（[driver.rs L71](https://github.com/daivy2333/StarryOS/blob/5d1a22689ed37d657c0ae39251a2e01980b50ec3/crates/uart_16550/src/async_/driver.rs#L71)） | 无抽象，直接 `use axhal/axtask` |
| 依赖 | embassy-sync、embassy_hal_internal | axtask、axhal |
| 测试 | `#[cfg(test)]` 单测（ring buffer readiness 等） | 无单测，feature 门控真机基准 |

## 互相可借鉴的点

SDMMC 值得借鉴：

- **代际快照**：比 register-recheck 更彻底的 lost-wakeup 防护，DMA 场景（网卡 RX）比 recheck 稳
- **取消边界文档**：每个 async API 的 doc comment 写清 drop 后果，区分 drop 时点
- **超时分层**：命令/数据/busy 三档超时 + 定时器兜底

UART 值得借鉴：

- **copier 常驻架构的取消安全性**：天然避免 fail-stop；写块设备异步驱动时无法照搬，必须处理取消
- **NAPI 中断合并**：SDMMC 单次传输不需要，网卡需要
- **可重复单测**：SDMMC 测试全是真机基准，无环境可复现的单测

## 总结

- UART 范式：面向字节流设备的**生产者-消费者**异步——copier 搬运、ring 缓冲、mask 中断、可移植抽象。解决"持续数据流不阻塞、不丢、不忙等"。
- SDMMC 范式：面向块设备的**请求-响应**异步——DMA 直连、条件等待、代际快照、fail-stop 取消。解决"一次大块传输不忙等、不踩已释放内存"。

两种范式的差异本质是设备语义差异（流 vs 事务），不是先进程度差异。

## 关联

- [SDMMC 驱动异步架构分析](sdmmc-async-architecture.md)：xianxw 实现的单侧深入分析
- [异步 UART 驱动总体架构](../异步串口/async-uart-driver-architecture.md)：UART 侧设计细节
