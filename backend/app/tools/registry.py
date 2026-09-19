from __future__ import annotations

import ast
import math
import operator
from typing import Any, Callable
from pydantic import BaseModel, Field

from ..rag.retriever import KnowledgeRetriever


class ToolCallRecord(BaseModel):
    tool: str
    input: str
    result: str
    error: str | None = None


# Safe Math AST Evaluator
_ALLOWED_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}

_ALLOWED_FUNCTIONS = {
    "sqrt": math.sqrt,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "log": math.log,
    "log10": math.log10,
    "exp": math.exp,
    "abs": abs,
    "round": round,
    "pow": pow,
    "ceil": math.ceil,
    "floor": math.floor,
}

_ALLOWED_CONSTANTS = {
    "pi": math.pi,
    "e": math.e,
}


def _safe_eval_node(node: ast.AST) -> Any:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError(f"Unsupported constant type: {type(node.value)}")

    elif isinstance(node, ast.BinOp):
        op_type = type(node.op)
        if op_type not in _ALLOWED_OPERATORS:
            raise ValueError(f"Unsupported binary operator: {op_type.__name__}")
        left = _safe_eval_node(node.left)
        right = _safe_eval_node(node.right)
        return _ALLOWED_OPERATORS[op_type](left, right)

    elif isinstance(node, ast.UnaryOp):
        op_type = type(node.op)
        if op_type not in _ALLOWED_OPERATORS:
            raise ValueError(f"Unsupported unary operator: {op_type.__name__}")
        operand = _safe_eval_node(node.operand)
        return _ALLOWED_OPERATORS[op_type](operand)

    elif isinstance(node, ast.Name):
        if node.id in _ALLOWED_CONSTANTS:
            return _ALLOWED_CONSTANTS[node.id]
        raise ValueError(f"Unknown variable or constant: '{node.id}'")

    elif isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id in _ALLOWED_FUNCTIONS:
            args = [_safe_eval_node(arg) for arg in node.args]
            return _ALLOWED_FUNCTIONS[node.func.id](*args)
        raise ValueError(f"Unsupported function call: {ast.dump(node.func)}")

    else:
        raise ValueError(f"Disallowed AST expression type: {type(node).__name__}")


def safe_calculate(expression: str) -> str:
    """Safely evaluates a math expression without eval() or system execution."""
    cleaned = expression.strip().replace("^", "**").replace("×", "*").replace("÷", "/")
    if not cleaned:
        return "Error: Empty expression"
    try:
        parsed = ast.parse(cleaned, mode="eval")
        result = _safe_eval_node(parsed.body)
        # Format floating points neatly
        if isinstance(result, float) and result.is_integer():
            return str(int(result))
        elif isinstance(result, float):
            return f"{result:.6f}".rstrip("0").rstrip(".")
        return str(result)
    except Exception as error:
        return f"Calculation Error: {error}"


class ToolRegistry:
    def __init__(self) -> None:
        self.retriever = KnowledgeRetriever()
        self._tools: dict[str, dict[str, Any]] = {}
        self._register_default_tools()

    def _register_default_tools(self) -> None:
        self.register(
            name="calculator",
            description="Safely compute mathematical and numerical expressions (e.g., '14 * 12 + sqrt(144)').",
            handler=self._tool_calculator,
        )
        self.register(
            name="document_search",
            description="Search user-uploaded knowledge documents and files for relevant context.",
            handler=self._tool_document_search,
        )
        self.register(
            name="knowledge_search",
            description="Query the general knowledge base and system guidance repository.",
            handler=self._tool_knowledge_search,
        )

    def register(self, name: str, description: str, handler: Callable[[str], str]) -> None:
        self._tools[name] = {"name": name, "description": description, "handler": handler}

    def list_tools(self) -> list[dict[str, str]]:
        return [{"name": data["name"], "description": data["description"]} for data in self._tools.values()]

    def execute(self, tool_name: str, tool_input: str) -> ToolCallRecord:
        if tool_name not in self._tools:
            return ToolCallRecord(
                tool=tool_name,
                input=tool_input,
                result="",
                error=f"Tool '{tool_name}' is not in the controlled registry. Available: {list(self._tools.keys())}",
            )
        try:
            handler = self._tools[tool_name]["handler"]
            output = handler(tool_input)
            return ToolCallRecord(tool=tool_name, input=tool_input, result=str(output))
        except Exception as error:
            return ToolCallRecord(tool=tool_name, input=tool_input, result="", error=f"Tool execution failed: {error}")

    def _tool_calculator(self, expression: str) -> str:
        return safe_calculate(expression)

    def _tool_document_search(self, query: str) -> str:
        res = self.retriever.retrieve(query, n_results=3)
        if res.has_results:
            return "\n\n".join(f"[{c.document_name}]: {c.text}" for c in res.chunks)
        return "No specific document match found in knowledge base."

    def _tool_knowledge_search(self, query: str) -> str:
        res = self.retriever.retrieve(query, n_results=2)
        return res.formatted_context or "No matching knowledge entries found."


tool_registry = ToolRegistry()
