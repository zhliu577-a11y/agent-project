# static —— 模型路由插件

实现 `model-router.v1`。路由顺序为：

1. `ModelGateway.use()` 设置的显式 provider；
2. 当前 agent role 对应的 `config/model.json` route；
3. `config/model.json` 的默认模型；
4. `fallback` 列表。

请求一旦开始流式输出，Gateway 不会切换到 fallback，避免混合两个模型的回答。
