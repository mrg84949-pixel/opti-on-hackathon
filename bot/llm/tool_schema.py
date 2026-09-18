from __future__ import annotations

import inspect
import json
import re
from typing import Any, Callable, get_args, get_origin

from bot.llm.context import TurnContext


def _python_type_to_json(annotation: Any) -> dict[str, Any]:
    if annotation is inspect.Parameter.empty:
        return {"type": "string"}
    origin = get_origin(annotation)
    if origin is not None:
        args = get_args(annotation)
        if origin is type(None) or str(origin) == "typing.Union":
            non_none = [a for a in args if a is not type(None)]
            if len(non_none) == 1:
                return _python_type_to_json(non_none[0])
        if origin is list:
            item = _python_type_to_json(args[0] if args else Any)
            return {"type": "array", "items": item}
    if annotation is str:
        return {"type": "string"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if annotation is bool:
        return {"type": "boolean"}
    return {"type": "string"}


def _parse_docstring(doc: str | None) -> tuple[str, dict[str, str]]:
    if not doc:
        return "", {}
    lines = doc.strip().splitlines()
    description_lines: list[str] = []
    param_desc: dict[str, str] = {}
    in_args = False
    for line in lines:
        stripped = line.strip()
        if stripped.lower().startswith("args:"):
            in_args = True
            continue
        if in_args:
            match = re.match(r"^(\w+):\s*(.+)$", stripped)
            if match:
                param_desc[match.group(1)] = match.group(2)
            elif stripped and not stripped.endswith(":"):
                in_args = False
                description_lines.append(stripped)
            continue
        if stripped:
            description_lines.append(stripped)
    return " ".join(description_lines).strip(), param_desc


def build_openai_tools(functions: list[Callable[..., Any]]) -> list[dict[str, Any]]:
    """Convert Python tool callables to OpenAI-compatible tool definitions."""
    tools: list[dict[str, Any]] = []
    for func in functions:
        sig = inspect.signature(func)
        description, param_docs = _parse_docstring(func.__doc__)
        properties: dict[str, Any] = {}
        required: list[str] = []
        for name, param in sig.parameters.items():
            if param.default is not inspect.Parameter.empty:
                schema = _python_type_to_json(param.annotation)
                if param.default is not None and param.default != "":
                    schema["default"] = param.default
                properties[name] = schema
            else:
                properties[name] = _python_type_to_json(param.annotation)
                required.append(name)
            if name in param_docs:
                properties[name]["description"] = param_docs[name]
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": func.__name__,
                    "description": description or func.__name__,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                    },
                },
            }
        )
    return tools


def build_tool_registry(functions: list[Callable[..., Any]]) -> dict[str, Callable[..., Any]]:
    return {func.__name__: func for func in functions}


async def execute_tool_call(
    registry: dict[str, Callable[..., Any]],
    name: str,
    arguments_json: str,
) -> str:
    func = registry.get(name)
    if func is None:
        return f"Неизвестный инструмент: {name}"
    try:
        args = json.loads(arguments_json) if arguments_json else {}
    except json.JSONDecodeError:
        return f"Некорректные аргументы для {name}"
    if not isinstance(args, dict):
        return f"Некорректные аргументы для {name}"
    try:
        result = func(**args)
        if inspect.isawaitable(result):
            result = await result
        return str(result)
    except TypeError as exc:
        return f"Ошибка вызова {name}: {exc}"
    except Exception as exc:
        return f"Ошибка инструмента {name}: {exc}"


def make_openai_tooling(ctx: TurnContext, tool_functions: list[Callable[..., Any]]) -> tuple[list[dict], dict[str, Callable]]:
    """Build OpenAI tools list and executable registry from make_tools output."""
    _ = ctx
    registry = build_tool_registry(tool_functions)
    return build_openai_tools(tool_functions), registry
