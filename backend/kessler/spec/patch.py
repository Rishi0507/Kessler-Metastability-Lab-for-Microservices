from __future__ import annotations

import io
import re
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.emitter import Emitter
from ruamel.yaml.error import YAMLError

_MAX_FLOW_ITEMS = 4


class PatchError(ValueError):
    pass


class _PaddedFlowEmitter(Emitter):
    _open_col = -1

    def write_indicator(
        self, indicator: Any, need_whitespace: Any, whitespace: bool = False, indention: bool = False
    ) -> None:
        if indicator == self.flow_map_end and self.column != self._open_col:
            need_whitespace = True
        super().write_indicator(indicator, need_whitespace, whitespace, indention)
        if indicator.lstrip(" ") == self.flow_map_start:
            self.whitespace = False
            self._open_col = self.column


_PADDED = re.compile(r"\{ +[^\s}]")
_TIGHT = re.compile(r"\{[^\s}]")


def _uses_padded_braces(text: str) -> bool:
    return len(_PADDED.findall(text)) >= len(_TIGHT.findall(text))


def _guess_indent(doc: Any) -> tuple[int, int, int]:
    found: dict[str, int] = {}

    def walk(node: Any, parent_col: int | None) -> None:
        if len(found) == 2:
            return
        if isinstance(node, CommentedMap) and not node.fa.flow_style():
            for key in node:
                _, col = node.lc.key(key)
                if parent_col is not None:
                    found.setdefault("mapping", col - parent_col)
                walk(node[key], col)
        elif isinstance(node, CommentedSeq) and not node.fa.flow_style():
            for i, item in enumerate(node):
                _, col = node.lc.item(i)
                if parent_col is not None:
                    found.setdefault("sequence", col - parent_col)
                walk(item, None)

    walk(doc, None)
    mapping = found.get("mapping", 2)
    sequence = found.get("sequence", mapping + 2)
    return mapping, sequence, max(0, sequence - 2)


def _new_value(value: Any, flow: bool) -> Any:
    if isinstance(value, dict):
        m = CommentedMap((k, _new_value(v, flow)) for k, v in value.items())
        if flow and len(value) <= _MAX_FLOW_ITEMS and not any(isinstance(v, (dict, list)) for v in value.values()):
            m.fa.set_flow_style()
        return m
    if isinstance(value, list):
        return CommentedSeq(_new_value(v, flow) for v in value)
    return value


def _dumper(text: str, indent: tuple[int, int, int]) -> YAML:
    y = YAML(typ="rt")
    y.preserve_quotes = True
    y.width = 4096
    mapping, sequence, offset = indent
    y.indent(mapping=mapping, sequence=sequence, offset=offset)
    if _uses_padded_braces(text):
        y.Emitter = _PaddedFlowEmitter
    return y


def apply_patch(yaml_text: str, ops: list[dict[str, Any]]) -> str:
    try:
        doc = YAML(typ="rt").load(yaml_text)
    except YAMLError as exc:
        raise PatchError(f"spec is not valid YAML: {exc}") from None
    if not isinstance(doc, CommentedMap):
        raise PatchError("spec must be a mapping")
    indent = _guess_indent(doc)
    flow = "{" in yaml_text
    for op in ops:
        if op.get("op") != "set":
            raise PatchError(f"unsupported op {op.get('op')!r}")
        path = op.get("path") or []
        if not path or any(p is None for p in path):
            raise PatchError(f"invalid path {path!r}")
        node: Any = doc
        for key in path[:-1]:
            if isinstance(node, list):
                if not isinstance(key, int) or not 0 <= key < len(node):
                    raise PatchError(f"bad index {key!r} in {path!r}")
                node = node[key]
            elif isinstance(node, dict):
                if node.get(key) is None:
                    node[key] = CommentedMap()
                node = node[key]
            else:
                raise PatchError(f"{key!r} in {path!r} is inside a scalar")
        last = path[-1]
        if isinstance(node, list):
            if not isinstance(last, int) or not 0 <= last < len(node):
                raise PatchError(f"bad index {last!r} in {path!r}")
        elif not isinstance(node, dict):
            raise PatchError(f"{last!r} in {path!r} is inside a scalar")
        node[last] = _new_value(op.get("value"), flow)
    out = io.StringIO()
    _dumper(yaml_text, indent).dump(doc, out)
    return out.getvalue()
