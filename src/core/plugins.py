"""Load user-provided Python functions behind a small, testable contract.

Custom plugins are expected to be deterministic for a given context. They
should not use wall-clock time, global random state, external mutable state,
or comparison-arm identifiers, and must not mutate shared experiment state.
When stochastic behavior is needed, plugins should derive it from the
deterministic values supplied in their context.
"""

from __future__ import annotations

import ast
import math
import statistics
from collections.abc import Callable, Mapping
from typing import Any

from .errors import PluginExecutionError, PluginValidationError

PluginCallable = Callable[[Mapping[str, Any]], Any]

PLUGIN_DETERMINISM_CONTRACT = (
    "Plugins must be deterministic for an equivalent context; avoid wall-clock "
    "time, global randomness, external mutable state, arm identity, and shared "
    "state mutation."
)


def plugin_determinism_requirements() -> tuple[str, ...]:
    """Return the user-facing determinism requirements for plugin authors."""
    return (
        "Be deterministic for a given plugin context.",
        "Do not depend on wall-clock time or global random state.",
        "Do not read or mutate external mutable state.",
        "Do not inspect comparison-arm identity.",
        "Do not mutate shared experiment state.",
    )

_SAFE_BUILTINS = {
    "abs": abs,
    "all": all,
    "any": any,
    "bool": bool,
    "dict": dict,
    "enumerate": enumerate,
    "float": float,
    "int": int,
    "len": len,
    "list": list,
    "max": max,
    "min": min,
    "range": range,
    "round": round,
    "set": set,
    "sorted": sorted,
    "str": str,
    "sum": sum,
    "tuple": tuple,
    "zip": zip,
}


def _safe_range(*arguments: int) -> range:
    result = range(*arguments)
    if len(result) > 100_000:
        raise ValueError("Plugin ranges cannot exceed 100,000 iterations.")
    return result


_SAFE_BUILTINS["range"] = _safe_range


class _PluginAstValidator(ast.NodeVisitor):
    """Reject imports and private-name access from uploaded plugin source."""

    def visit_Import(self, node: ast.Import) -> None:
        raise PluginValidationError(
            "Plugin imports are disabled; math and statistics are provided."
        )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        raise PluginValidationError(
            "Plugin imports are disabled; math and statistics are provided."
        )

    def visit_Global(self, node: ast.Global) -> None:
        raise PluginValidationError("Plugin global statements are disabled.")

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        raise PluginValidationError("Plugin nonlocal statements are disabled.")

    def visit_While(self, node: ast.While) -> None:
        raise PluginValidationError("Plugin while loops are disabled.")

    def visit_Name(self, node: ast.Name) -> None:
        if node.id.startswith("_"):
            raise PluginValidationError(
                "Plugin code cannot access private names."
            )
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("_"):
            raise PluginValidationError(
                "Plugin code cannot access private attributes."
            )
        self.generic_visit(node)


def load_plugin_function(
    source_code: str,
    entrypoint: str,
    *,
    plugin_name: str,
) -> PluginCallable:
    """Compile and return a user function with restricted global access."""
    if not source_code.strip():
        raise PluginValidationError(f"{plugin_name} source code is empty.")
    if not entrypoint.isidentifier() or entrypoint.startswith("_"):
        raise PluginValidationError(
            f"{plugin_name} entrypoint is not a valid public function name."
        )

    try:
        syntax_tree = ast.parse(source_code, filename=f"<{plugin_name}>")
    except SyntaxError as exc:
        raise PluginValidationError(
            f"{plugin_name} contains invalid Python syntax: {exc.msg}."
        ) from exc

    _PluginAstValidator().visit(syntax_tree)
    namespace: dict[str, Any] = {
        "__builtins__": _SAFE_BUILTINS,
        "math": math,
        "statistics": statistics,
    }

    try:
        exec(
            compile(syntax_tree, filename=f"<{plugin_name}>", mode="exec"),
            namespace,
        )
    except Exception as exc:
        raise PluginValidationError(
            f"{plugin_name} could not be initialized: {exc}."
        ) from exc

    function = namespace.get(entrypoint)
    if not callable(function):
        raise PluginValidationError(
            f"{plugin_name} must define callable '{entrypoint}'."
        )

    def wrapped(context: Mapping[str, Any]) -> Any:
        try:
            return function(context)
        except Exception as exc:
            raise PluginExecutionError(
                f"{plugin_name} failed during execution: {exc}."
            ) from exc

    return wrapped
