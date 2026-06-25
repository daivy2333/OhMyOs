# Rust 异步编程核心概念

**日期**：2026-06-25
**标签**：rust, async, tokio

## Future trait

Rust 异步的核心抽象是 `Future` trait：

```rust
pub trait Future {
    type Output;
    fn poll(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Self::Output>;
}
```

`poll` 返回 `Poll::Ready(T)` 或 `Poll::Pending`，由执行器反复调用。

## Waker 机制

`poll` 返回 `Pending` 时，future 通过 `Waker` 通知执行器"我准备好了"。这避免了忙轮询。执行器可在任务 Pending 时挂起线程或切换任务。

## async/await

`async fn` 与 `async { ... }` 块会被展开为状态机。每个 `.await` 是状态机的一个分支。
