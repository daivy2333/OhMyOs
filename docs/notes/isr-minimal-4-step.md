# ISR 极简 4 步流程

**日期**：2026-06-25
**标签**：rust, isr, riscv, interrupt, uart, dependency-injection

> 来源：StarryOS `uart_16550/src/async_/isr.rs:71-92` 通读。
> 范围：NS16550 ISR handler 的 4 步流程 + 依赖注入设计。

## 4 步流程

```rust
pub fn uart_isr_handler(_irq, base, fn_disable_rx, fn_disable_tx) {
    unsafe {
        let regs = IsrRegisters::new(base);
        let isr = regs.read_isr();  // 1. 读 ISR 寄存器
        match isr.interrupt_type() {
            Some(RDR|RTI) => {
                fn_disable_rx();     // 2. 禁用对应中断
                RX_WAKER.wake();     // 3. 唤醒 waker
            }
            Some(THRE) => {
                fn_disable_tx();
                TX_WAKER.wake();
                if regs.read_lsr().contains(TEMT) {
                    DRAIN_WAKER.wake();
                }
            }
            _ => {}
        }
        // 4. 立即返回
    }
}
```

全程 ~1.5 µs。

## 3 个中断类型

NS16550 ISR 寄存器的 bits 2-1 编码：

| bits 2:1 | 类型 | 处理 |
|---|---|---|
| 00 | ModemStatus | 忽略（_ => {}）|
| 01 | THRE | TX + 条件 DRAIN |
| 10 | RDR | RX |
| 11 | RTI | RX（与 RDR 合并）|

RDR + RTI 合并：两者都意味着"有 RX 数据"。

## IsrRegisters：锁无关 MMIO 读取

```rust
pub(crate) struct IsrRegisters {
    base: NonNull<u8>,
}

impl IsrRegisters {
    pub(crate) unsafe fn read_isr(&self) -> ISR {
        let ptr = self.base.as_ptr().add(offsets::ISR);
        ISR::from_bits_retain(ptr.read_volatile())
    }
}
```

不走 `Uart16550::isr()`（需要 SpinNoIrq 锁，违反 ISR 极简）。
直接 `read_volatile` 读 MMIO 寄存器。

`read_volatile` 防止编译器优化掉寄存器读。

## DRAIN_WAKER 条件唤醒

```rust
if regs.read_lsr().contains(LSR::TRANSMITTER_EMPTY) {
    DRAIN_WAKER.wake();
}
```

| 时机 | THR_EMPTY | TEMT | 唤醒 |
|---|---|---|---|
| THRE 中断 + 移位寄存器还在发 | 1 | 0 | TX |
| THRE 中断 + 移位寄存器也空 | 1 | 1 | TX + DRAIN |

tcdrain 关心"全部数据离开芯片到线缆"，必须 TEMT 才算完成。

## 依赖注入：OS 解耦

`fn_disable_rx: fn()` 和 `fn_disable_tx: fn()` 是函数指针参数。

```rust
// uart_init.rs:171
fn uart_isr_wrapper(_irq: usize) {
    uart_isr_handler(_irq, base,
        || UART_PORT.update_ier(IER::empty(), IER::DATA_READY),
        || UART_PORT.update_ier(IER::empty(), IER::THR_EMPTY),
    );
}
```

为什么？
- uart_16550 crate 不知道 StarryOS 的 UART_PORT
- 直接调 OS API = OS 耦合
- 函数指针注入 = 解耦

| 维度 | 直接调用 | 注入 |
|---|---|---|
| 解耦 | ❌ | ✅ |
| 性能 | 零开销 | ~1ns 函数指针 |
| 类型安全 | 编译期 | 编译期 |

## Q8.2 修复

之前 ISR 中调 `uart.isr()` + `disable_*_intr()` 走 SpinNoIrq 锁。

Q8.2 修复：拆出 `uart_isr_handler` + 闭包，锁只在 update_ier 一处。

## 经验

- ISR 极简 4 步：读 ISR / 禁中断 / wake / 返回
- IsrRegisters 绕过高级 API + read_volatile
- 依赖注入让 uart_16550 与 OS 解耦
- 3 个独立 waker 对应 3 个语义角色
- DRAIN 条件唤醒省一次 wake 但保证 tcdrain 正确