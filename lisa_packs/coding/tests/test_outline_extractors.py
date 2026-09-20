"""Unit tests for the language-specific structural-outline extractors.

These exercise the pure ``_<lang>_outline`` functions directly — they operate on
source text only, so no ToolExecutionContext, worktree, or artifact store is
required. The artifact-scoped dispatch path is covered in test_git_workflow.py.
"""

from __future__ import annotations

from lisa_packs.coding.tools.outline_source.outline_source import (
    _go_outline,
    _python_outline,
    _rust_outline,
    _ts_outline,
)


def test_python_outline_captures_top_level_classes_functions_methods() -> None:
    src = (
        "import os\n"
        "\n"
        "def top() -> None:\n"
        "    pass\n"
        "\n"
        "async def atop() -> None:\n"
        "    pass\n"
        "\n"
        "class Svc:\n"
        "    def run(self) -> None:\n"
        "        pass\n"
        "\n"
        "    async def arun(self) -> None:\n"
        "        pass\n"
    )
    assert _python_outline(src) == [
        {"kind": "function", "name": "top", "line": 3, "async": False},
        {"kind": "function", "name": "atop", "line": 6, "async": True},
        {"kind": "class", "name": "Svc", "line": 9},
        {"kind": "method", "name": "run", "line": 10, "async": False},
        {"kind": "method", "name": "arun", "line": 13, "async": True},
    ]


def test_ts_outline_captures_declarations_and_arrow_consts() -> None:
    src = (
        "export class Server {}\n"
        "interface Config {}\n"
        "type Opt = string;\n"
        "const boot = async () => {};\n"
        "export default function app() {}\n"
    )
    assert _ts_outline(src) == [
        {"kind": "declaration", "name": "Server", "line": 1},
        {"kind": "declaration", "name": "Config", "line": 2},
        {"kind": "declaration", "name": "Opt", "line": 3},
        {"kind": "declaration", "name": "boot", "line": 4},
        {"kind": "declaration", "name": "app", "line": 5},
    ]


def test_go_outline_captures_funcs_methods_and_named_types() -> None:
    src = (
        "package main\n"
        "\n"
        "func main() {}\n"
        "func (s *Server) Start() error { return nil }\n"
        "type Server struct{}\n"
        "type Handler interface{}\n"
        "func helper[T any](v T) {}\n"
    )
    assert _go_outline(src) == [
        {"kind": "function", "name": "main", "line": 3},
        {"kind": "method", "name": "Start", "line": 4},
        {"kind": "struct", "name": "Server", "line": 5},
        {"kind": "interface", "name": "Handler", "line": 6},
        {"kind": "function", "name": "helper", "line": 7},
    ]


def test_go_outline_ignores_indented_and_var_const() -> None:
    src = (
        "package main\n"
        "\n"
        "func outer() {\n"
        "\tinner := func() {}\n"
        "}\n"
        "var cfg Config\n"
    )
    items = _go_outline(src)
    assert items == [{"kind": "function", "name": "outer", "line": 3}]


def test_rust_outline_captures_fns_and_items() -> None:
    src = (
        "pub struct Config {}\n"
        "impl Config {\n"
        "    pub async fn load() {}\n"
        "}\n"
        "fn main() {}\n"
        "pub(crate) mod util;\n"
        "pub enum State {}\n"
        "unsafe trait Raw {}\n"
    )
    assert _rust_outline(src) == [
        {"kind": "struct", "name": "Config", "line": 1},
        {"kind": "impl", "name": "Config", "line": 2},
        {"kind": "function", "name": "load", "line": 3},
        {"kind": "function", "name": "main", "line": 5},
        {"kind": "mod", "name": "util", "line": 6},
        {"kind": "enum", "name": "State", "line": 7},
        {"kind": "trait", "name": "Raw", "line": 8},
    ]
