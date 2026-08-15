# SDMMC 异步驱动评审意见

**日期**：2026-08-14
**标签**：rust, async, driver, sdmmc, review, testability, portability

> 评审对象：[xianxw/Final-NO-SDMMC](https://github.com/xianxw/Final-NO-SDMMC) `modules/simple-sdmmc-extended/`（revision [`f0bdece`](https://github.com/xianxw/Final-NO-SDMMC/tree/f0bdecedf50047a4efee598ee39080e109f2f25e)）
> 评审范围：异步接口层（`sdmmc.rs`）及配套 `cmd.rs`、`dma.rs`。
> 目的：在不要求改变现有架构的前提下，用最小改动获得可测试性和可扩展性。

## 先肯定做得好的地方

以下三点是这次实现的亮点，建议保持：

- **同步/异步双路径**：同一份传输逻辑提供 `read_blocks` 与 `read_blocks_async` 两个入口（[sdmmc.rs L1263 / L1290](https://github.com/xianxw/Final-NO-SDMMC/blob/f0bdecedf50047a4efee598ee39080e109f2f25e/modules/simple-sdmmc-extended/src/sdmmc.rs#L1263)），上层可按需选择，切换成本低。
- **代际快照防丢唤醒**：`IdmacCompletion` 用 generation + 原子位快照解决"IRQ 先于等待者注册"的竞态（[sdmmc.rs L53](https://github.com/xianxw/Final-NO-SDMMC/blob/f0bdecedf50047a4efee598ee39080e109f2f25e/modules/simple-sdmmc-extended/src/sdmmc.rs#L53)），比单纯 register-recheck 更彻底。
- **fail-stop 取消安全**：DMA 提交后 drop future 会停止 IDMAC 并 fault 驱动（[sdmmc.rs L295](https://github.com/xianxw/Final-NO-SDMMC/blob/f0bdecedf50047a4efee598ee39080e109f2f25e/modules/simple-sdmmc-extended/src/sdmmc.rs#L295)），防止 DMA 访问已释放描述符。

以下意见按优先级排列。P0 是测试缺失，几乎无争议；P1 是解耦与扩展性，为下一步留路。

## P0-1：可复现测试缺失，main 分支无回归兜底

**事实**

- `modules/simple-sdmmc-extended/` 无 `#[cfg(test)]`、无 tests 目录。
- 测试代码（`sdmmc_concurrency_test.rs` 1046 行、`sdmmc_write_performance_test.rs` 1525 行）以 feature 门控的形式存在于独立分支（`final-test` / `final-write-test`），未合入 main。

**影响**

main 分支上任何改动都不会被测试拦截；测试分支与 main 会漂移，测试迟早对不上主线代码。

**建议**

把不碰硬件的纯逻辑层抽出来做 host 可测单元：

- `cmd.rs` 的 `build()` 位拼装（[cmd.rs L91](https://github.com/xianxw/Final-NO-SDMMC/blob/f0bdecedf50047a4efee598ee39080e109f2f25e/modules/simple-sdmmc-extended/src/cmd.rs#L91)）——命令编码规则可断言
- `dma.rs` 的 IDMAC 描述符链构建（[dma.rs L140](https://github.com/xianxw/Final-NO-SDMMC/blob/f0bdecedf50047a4efee598ee39080e109f2f25e/modules/simple-sdmmc-extended/src/dma.rs#L140)）——OWN/FS/LD/CH 位组合可断言
- `regs.rs` 的位域解析——寄存器语义可断言

这些逻辑不依赖硬件，在任何环境都能跑。同类做法可参考 [daivy2333/StarryOS 的 ring_buffer.rs 单测](https://github.com/daivy2333/StarryOS/blob/5d1a22689ed37d657c0ae39251a2e01980b50ec3/crates/uart_16550/src/async_/ring_buffer.rs#L310)（readiness、wrap-around 等纯逻辑测试）。

## P0-2：完成状态机与硬件耦合，无法单测

**事实**

`IdmacCompletion` 的语义（begin_transfer / record_irq / snapshot_bits，[sdmmc.rs L53-106](https://github.com/xianxw/Final-NO-SDMMC/blob/f0bdecedf50047a4efee598ee39080e109f2f25e/modules/simple-sdmmc-extended/src/sdmmc.rs#L53)）本身是纯状态逻辑，但依赖 `axhal::time` 和全局 `static`。

**影响**

等待超时、代际匹配、快照冲突这些最容易出 bug 的逻辑无法在没有真机时验证。

**建议**

把 `IdmacCompletion` 的状态机做成不依赖硬件的纯类型：传入 `(rintsts_bits, idsts_bits)` 和 generation，返回状态判断结果。这样可以在测试里喂"错误位组合""旧代际快照""IRQ 与快照乱序"等场景，覆盖真实竞态。

## P1-1：全局 static 单等待者假设，限制多实例与并发

**事实**

- `IDMAC_WAIT_QUEUE`、`IDMAC_DONE_FLAG`、`SDMMC_REGS_BASE` 都是 `static`（[sdmmc.rs L45-49](https://github.com/xianxw/Final-NO-SDMMC/blob/f0bdecedf50047a4efee598ee39080e109f2f25e/modules/simple-sdmmc-extended/src/sdmmc.rs#L45)）。
- `dma_irq_handler` 用 `notify_one(false)` 只唤醒一个等待者（[sdmmc.rs L2155](https://github.com/xianxw/Final-NO-SDMMC/blob/f0bdecedf50047a4efee598ee39080e109f2f25e/modules/simple-sdmmc-extended/src/sdmmc.rs#L2155)）。

**影响**

第二个控制器实例、或并发 DMA 传输，会直接撞上"单等待者"假设。

**建议**

全局 static 改为实例内持有（`SdMmc` 自带等待队列与完成快照），`dma_irq_handler` 通过实例引用访问。多实例场景无需改等待语义即可支持。

## P1-2：直接绑定 axhal/axtask，换平台或调度器需全量改动

**事实**

`SdMmc` 直接调用 `axhal::time::monotonic_time`、`axtask::WaitQueue`，无抽象层。

**影响**

- 等待逻辑与寄存器读写绑死，换平台（如新板卡、QEMU 变体）或换调度器（如接入其他执行器）要全量改动。
- 与 P0-2 是同一个根因：时间来源和等待原语没有参数化。

**建议**

不要求完整抽象，只参数化两个最小接口：

- 时间来源：`fn now() -> Duration` 或等价，替代直接调 `axhal::time`
- 等待原语：条件等待 + 超时，替代直接调 `axtask::WaitQueue`

改动量小，但能同时解锁单测（喂假时间/假队列）和平台迁移。

## 验证方式说明

以上建议的验收标准：

- P0-1：`cargo test` 在无硬件环境通过（cmd/dma/regs 纯逻辑断言）
- P0-2：状态机测试覆盖 IRQ 乱序、旧代际快照、超时场景
- P1-1：双实例注册后各自独立完成传输
- P1-2：换一个 mock 时间/队列实现，逻辑不改

真机基准（并发读、写性能）仍然是验证 DMA 时序与 IRQ 唤醒延迟的必要手段，建议作为 feature 合入 main 并在 CI 中编译验证，而不是保留在独立分支（还是看实际情况处理吧）。
不知道你用ai的频率和经验如何，但是我觉得测试都应该是保证代码质量和功能正确的重要手段，用各种各样的测试去覆盖边界。
