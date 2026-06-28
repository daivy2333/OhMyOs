# Async/Await 异步运行时

**日期**：2026-06-25
**标签**：rust, async, future, waker, executor, state-machine

> 来源：深度讲解 async/await 异步特性 + Q&A。
> 范围：Rust 异步运行时模型 + 本项目实践 + SRP 在异步的应用。

## Future trait 与 Pin

```rust
pub trait Future {
    type Output;
    fn poll(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Self::Output>;
}

pub enum Poll<T> {
    Ready(T),
    Pending,
}
```

Future = 状态机 + 唤醒机制。一个值表示"将来某个时刻可能完成的计算"。

**Future 是 lazy 的**

Future 不会自动执行，要调度器反复 poll。

- 零开销抽象（编译器内联）
- 调度器完全掌控执行时机
- 跨平台/跨 runtime 可移植

**Pin 阻止 future 移动**

`Pin<&mut Self>` 阻止 future 内存地址在 await 之间变化。

原因：自引用 future（如跨 await 持有借用）如果可移动，引用会失效。

```rust
async fn bad() {
    let x = [1, 2, 3];
    let y = &x[0];           // y 借 x 的生命周期
    some_async_op(y).await;  // 跨 await 持有借用 → 需要 Pin
}
```

## async/await 状态机展开

每个 await 点 = 一个状态。编译器把 async fn 转换成 enum + impl Future。

```rust
async fn fetch_data() -> Result<String, Error> {
    let s = read_from_disk().await?;
    let p = parse(s).await?;
    Ok(p)
}
```

编译器展开（伪代码）：

```rust
enum FetchData {
    Start,
    WaitingRead { fut: ReadFuture },
    WaitingParse { fut: ParseFuture, s: String },
    Done,
}

impl Future for FetchData {
    type Output = Result<String, Error>;
    fn poll(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Self::Output> {
        match self.get_mut() {
            Self::Start => {
                let fut = read_from_disk();
                *self = Self::WaitingRead { fut };
                self.poll(cx)
            }
            Self::WaitingRead { fut } => {
                match Pin::new(fut).poll(cx) {
                    Poll::Ready(Ok(s)) => {
                        let fut = parse(s.clone());
                        *self = Self::WaitingParse { fut, s };
                        self.poll(cx)
                    }
                    Poll::Ready(Err(e)) => Poll::Ready(Err(e)),
                    Poll::Pending => Poll::Pending,
                }
            }
            // ...
        }
    }
}
```

`future.await` 等价于：

1. poll future
2. Ready → 继续
3. Pending → 注册 waker，让出控制权

## Waker 机制

```rust
pub struct Waker { raw: *const () }
impl Waker {
    pub fn wake(self);
    pub fn wake_by_ref(&self);
    pub fn clone(&self) -> Waker;
}
```

**作用**

Future 一直 Pending 时，谁通知调度器"可以重 poll 了"？Waker。

**传递**

通过 `Context<'_>`：poll 时传给 future，含 `&Waker`。

Future 在 Pending 前必须 register waker：

```rust
fn poll(...) -> Poll<()> {
    if data_available() { return Poll::Ready(()); }
    WAKER.register(cx.waker());
    Poll::Pending
}
```

**丢失唤醒反模式**

```rust
fn poll(...) -> Poll<()> {
    if check_data() { return Poll::Ready(()); }
    cx.waker().wake_by_ref();  // ❌ 立即唤醒自己 → yield storm
    Poll::Pending
}
```

正确做法：只 register waker，不立即唤醒：

```rust
fn poll(...) -> Poll<()> {
    if check_data() { return Poll::Ready(()); }
    WAKER.register(cx.waker());
    Poll::Pending
}
```

## Executor 组件

StarryOS 用 axtask::future：

- `block_on`：同步等待 future 完成
- `poll_io`：ISR 唤醒的标准模式
- `register_irq_waker`：在 PollSet 注册 waker

**poll_io 标准模式**

```rust
async fn wait_for_data() -> Data {
    poll_fn(|cx| {
        if data_ready() { return Poll::Ready(read_data()); }
        register_irq_waker(IRQ_NUM, cx.waker());
        Poll::Pending
    }).await
}
```

ISR 触发 → `register_irq_waker` 注册的 waker 被唤醒 → task 重 poll → 拿到数据。

**poll_fn 闭包 future**

```rust
pub fn poll_fn<F, T>(f: F) -> PollFn<F>
where F: FnMut(&mut Context<'_>) -> Poll<T>
```

把闭包包装成 Future。每次 poll 调用闭包。

## 本项目实践

**RX copier 主循环**

```rust
async fn rx_copier_loop(&self) {
    let mut read_buf = [0u8; COPIER_BUF_SIZE];
    let mut consecutive = 0u32;
    
    loop {
        poll_fn(|cx| {
            let batch = if consecutive >= NAPI_THRESHOLD {
                NAPI_BATCH_SIZE
            } else {
                COPIER_BUF_SIZE
            };
            
            let total = self.uart.receive_bytes(&mut read_buf[..batch]);
            
            if total > 0 { self.rx.push_batch(&read_buf[..total]); }
            
            // NAPI 状态机
            if consecutive >= NAPI_THRESHOLD {
                if total > 0 { consecutive += 1; }
                else {
                    consecutive = 0;
                    self.uart.update_ier(IER::DATA_READY, IER::empty());
                }
            } else {
                consecutive = if total > 0 { consecutive + 1 } else { 0 };
            }
            
            if consecutive < NAPI_THRESHOLD {
                self.uart.update_ier(IER::DATA_READY, IER::empty());
            }
            
            RX_WAKER.register(cx.waker());
            
            if total > 0 { Poll::Ready(total) } else { Poll::Pending }
        }).await;
    }
}
```

模式识别：

- 永远 `loop{}` 包裹
- poll_fn 闭包做单次工作
- 数据到达 → Ready
- 数据未到 → register waker + Pending

**TtyRead 同步读取**

```rust
impl<R, W, U> TtyRead for AsyncUartReader<R, W, U> {
    fn read(&mut self, buf: &mut [u8]) -> usize {
        self.driver.rx.pop(buf)  // 同步路径：数据已在 ring
    }
}
```

不是 async：数据已在 ring 里，copier 已填充。

**flush 双检查**

```rust
async fn flush(&mut self) -> Result<(), Self::Error> {
    poll_fn(|cx| {
        let c = self.driver.tx_completion();
        if c.is_drained() { return Poll::Ready(Ok(())); }
        
        if !c.ring_empty || c.copier_active || c.staged_bytes > 0 {
            self.driver.tx.register_waker(cx.waker());
        }
        if c.staged_bytes == 0 && !c.copier_active && c.ring_empty {
            DRAIN_WAKER.register(cx.waker());
        }
        
        // 双检查：注册 waker 后状态可能已变
        let c2 = self.driver.tx_completion();
        if c2.is_drained() { Poll::Ready(Ok(())) } else { Poll::Pending }
    }).await
}
```

为什么双检查：避免"检查时未就绪、注册 waker 时已就绪"导致的丢失唤醒。

**spawn 启动模式**

```rust
pub fn start_rx_copier(&'static self) {
    R::spawn(
        async move {
            self.rx_copier_loop().await;
        },
        "uart-rx-copier",
    );
}
```

`async move` 把 self 所有权移入闭包。`&'static self` 保证生命周期足够。

## 为什么 waker 不返回值

waker 是通知机制，不是执行机制。它只说"该任务可以重 poll 了"，真正的检查由 poll 负责。

**违反 SRP 的代价**

如果让 waker 多做一件事（返回 Ready + 值）：

| 破坏点 | 说明 |
|---|---|
| ISR 极简破坏 | ISR 必须知道数据 + 搬到 ring buffer |
| 状态竞争 | ISR 与 copier 同时操作 ring buffer |
| Future 组合性丧失 | `select!` 无法独立 poll |
| 内存安全破洞 | 值从哪来？分配在 self 内 |

**性能对比**

```
waker.poll + executor.poll      ← 当前设计
├ 路径长 1 次 poll（~200ns）
└ 各模块单一职责

waker.poll_and_ready            ← 假设方案
├ 路径短 1 次 poll（节省 ~200ns）
└ ISR 极简破坏 + 状态竞争 + 组合性丧失
```

省 200ns 换整套架构破坏。

**SRP 分工**

| 模块 | 单一职责 |
|---|---|
| waker | 通知 |
| poll | 检查 + 返回值 + 状态突变 |
| executor | 调度 |
| ISR | 硬件事件捕获 |

性能优化不能以破坏架构为代价。

## 易踩的坑

**阻塞操作**

```rust
async fn bad() {
    std::thread::sleep(Duration::from_secs(1));  // ❌ 阻塞 executor
}
```

**死循环**

```rust
loop {
    poll_fn(|cx| {
        // 没 register waker
        Poll::Pending  // ❌ 永远 Pending
    }).await;
}
```

**多次 poll 副作用**

```rust
poll_fn(|cx| {
    println!("hi");  // ❌ 每次 poll 都打印
    Poll::Pending
}).await;
```

**非 Send 跨 await**

```rust
async fn bad() {
    let rc = Rc::new(42);  // Rc 不是 Send
    some_async_op().await;  // 跨 await 持有 → 编译错误
}
```

## 关键判断模板

```
写异步代码时：
1. 每个 await 点会"挂起"，保存状态
2. 挂起前必须 register waker
3. waker.wake() 后调度器会重 poll
4. Pending 不能"循环等待"——必须等事件
5. 数据到达是事件，ISR 是事件，timer 是事件
6. 阻塞操作不能用——破坏调度
7. 非 Send 数据不能在 await 持有
8. waker 只通知，poll 才检查（SRP）
```

## 经验

- `Future` = 状态机 + waker
- `async/await` 是语法糖，每个 await = 一个状态
- `Pin` 防止自引用 future 被移动
- 必须 register waker 才能 Pending
- 阻塞操作破坏 async 调度
- 双检查避免丢失唤醒
- waker 与 poll 是 SRP 的运行时体现
- 本项目：copier 是无限循环的 poll_fn 状态机
