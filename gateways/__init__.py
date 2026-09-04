# gateways —— 网关层：内核与插件之间的统一入口。
#
# 目前有两个网关，边界都是"内核只面向网关、不接触具体插件"：
#   McpGateway   所有 MCP 插件（plugins/mcp/*）的连接、命名与调用路由
#   HookGateway  所有钩子插件（plugins/hooks/*）的生命周期事件扇出
