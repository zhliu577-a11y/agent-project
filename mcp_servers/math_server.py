# mcp_servers/math_server.py —— 示例 MCP 服务器：安全计算器（不使用 eval）
import ast
import operator
from typing import Any

from mcp.server.mcpserver import MCPServer

server = MCPServer(name="math-server")

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
}


def _eval_node(node: ast.AST) -> Any:
    """只允许数字和 + - * / ** % 以及括号，杜绝任意代码执行。"""
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        return _BIN_OPS[type(node.op)](_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.UAdd):
        return +_eval_node(node.operand)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_eval_node(node.operand)
    raise ValueError("不支持的表达式")


@server.tool()
def calculate(expression: str) -> str:
    """计算数学表达式，支持 + - * / ** % 和括号，例如 '17 * 23 + 1'。"""
    try:
        tree = ast.parse(expression, mode="eval")
        return str(_eval_node(tree.body))
    except Exception as exc:
        return f"计算失败: {exc}"


if __name__ == "__main__":
    server.run()
