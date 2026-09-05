# deepseek —— 模型插件示例

展示了 `type: "model"` 的写法：插件目录里的 `model.py` 实现
`core.model.ModelAdapter`，工厂 `create_model` 返回适配器实例。

模型插件由 Harness 在启动时根据 `AGENT_MODEL`（默认 `deepseek`）选出并
惰性实例化，实例化时读取 `DEEPSEEK_API_KEY` 等环境变量，行为与旧 `models/`
实现完全一致。要接入其它 OpenAI 兼容服务，复制本目录改配置即可。
