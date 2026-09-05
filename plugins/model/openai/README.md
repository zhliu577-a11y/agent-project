# openai —— 模型插件示例（第二家提供方）

与 `plugins/model/deepseek` 结构完全一致，区别只在读取的环境变量：

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `OPENAI_API_KEY` | 无（必填） | OpenAI API Key |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | 接口地址 |
| `OPENAI_MODEL` | `gpt-4o-mini` | 模型名 |
| `OPENAI_TIMEOUT` | `60` | 超时（秒） |
| `OPENAI_MAX_RETRIES` | `3` | 重试次数 |

使用方式：

```powershell
$env:AGENT_MODEL = "openai"   # 或在 .env 里写 AGENT_MODEL=openai
.venv\Scripts\python.exe main.py
```

这就是“换模型 = 复制插件目录 + 改配置”：接通义、本地 vLLM 等任何
OpenAI 兼容服务时，复制本目录并修改 `OPENAI_*` 对应的变量即可。
