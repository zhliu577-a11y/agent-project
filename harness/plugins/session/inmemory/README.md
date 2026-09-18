# inmemory —— 会话存储插件（内存）

`type: "session"` 的内存后端：历史与 checkpoint 都存在进程内字典里，
**重启即失**，适合测试、临时会话或演示“换存储 = 换插件”。

## 配置

```text
config/session.json: { "store": "inmemory" }
环境变量:              SESSION_STORE=inmemory
```

接口与 jsonl 后端完全一致（`load/save` + checkpoint 三方法），切换不影响内核。
