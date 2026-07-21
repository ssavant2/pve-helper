"""Every name a module reads must actually resolve.

`from x import *` makes an undefined global look imported: the reader sees a
wildcard that could plausibly supply it, and ruff cannot tell the difference
either, so F405 has to be switched off wherever one is used. Five names were
left behind by the guests/ module split under exactly that cover, and one of
them 500'd every console page load because no test rendered an object-scoped
guest page.

The view modules now import by name and F405 guards them again, but the two
re-export facades (`core/views/__init__.py`, `core/views/common.py`) still
carry wildcards and always will. This test covers what the linter cannot: it
resolves every name against what the module really exports at runtime, so a
name dropped by the *next* extraction fails here instead of in production.

The analysis is deliberately conservative — a name bound anywhere in the file
counts as bound everywhere in it — because a false alarm costs a maintainer more
than the narrow extra coverage would buy.
"""

from __future__ import annotations

import ast
import builtins
import importlib
import pkgutil
from pathlib import Path

from django.test import SimpleTestCase

import core

_PACKAGES = ("core.views", "core.services", "core.templatetags")


def _iter_modules():
    root = Path(core.__file__).parent
    for package_name in _PACKAGES:
        package = importlib.import_module(package_name)
        for info in pkgutil.walk_packages(package.__path__, prefix=f"{package_name}."):
            if info.ispkg:
                continue
            yield info.name
    for path in sorted(root.glob("*.py")):
        if path.stem.startswith("tests") or path.stem == "__init__":
            continue
        yield f"core.{path.stem}"


class _Bindings(ast.NodeVisitor):
    """Collect every name the file binds, at any scope depth."""

    def __init__(self):
        self.bound: set[str] = set()
        self.loaded: dict[str, int] = {}

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load):
            self.loaded.setdefault(node.id, node.lineno)
        else:
            self.bound.add(node.id)

    def visit_arg(self, node):
        self.bound.add(node.arg)
        self.generic_visit(node)

    def visit_alias(self, node):
        # `import a.b` binds `a`; `import a.b as c` and `from x import y` bind the
        # asname/name as written.
        self.bound.add((node.asname or node.name).split(".")[0])

    def visit_ExceptHandler(self, node):
        if node.name:
            self.bound.add(node.name)
        self.generic_visit(node)

    def visit_Global(self, node):
        self.bound.update(node.names)

    def visit_Nonlocal(self, node):
        self.bound.update(node.names)

    def _visit_definition(self, node):
        self.bound.add(node.name)
        self.generic_visit(node)

    visit_FunctionDef = _visit_definition
    visit_AsyncFunctionDef = _visit_definition
    visit_ClassDef = _visit_definition

    def visit_MatchAs(self, node):
        if node.name:
            self.bound.add(node.name)
        self.generic_visit(node)

    def visit_MatchStar(self, node):
        if node.name:
            self.bound.add(node.name)
        self.generic_visit(node)

    def visit_MatchMapping(self, node):
        if node.rest:
            self.bound.add(node.rest)
        self.generic_visit(node)


class ModuleGlobalsResolveTests(SimpleTestCase):
    def test_every_loaded_name_resolves(self):
        unresolved = []
        for module_name in _iter_modules():
            module = importlib.import_module(module_name)
            source_path = getattr(module, "__file__", None)
            if not source_path:
                continue
            collector = _Bindings()
            collector.visit(ast.parse(Path(source_path).read_text(encoding="utf-8")))
            for name, lineno in sorted(collector.loaded.items()):
                if name in collector.bound or hasattr(builtins, name) or hasattr(module, name):
                    continue
                unresolved.append(f"{module_name}:{lineno}: {name}")
        self.assertEqual(
            unresolved,
            [],
            "Names read but never defined, imported or exported by a star import:\n  " + "\n  ".join(unresolved),
        )
