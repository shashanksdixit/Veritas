"""Reachability walker for the read-only boundary test (T112, FR-026).

Starting from a graph node's function, ``walk`` follows every reference a
Python object graph exposes, depth-first with a visited set, and reports each
object that grants a write or execute capability, together with the path that
reached it. The edges it follows:

* functions: closure cells, ``__defaults__``, ``__kwdefaults__``;
* ``functools.partial``: ``func``, ``args``, ``keywords``;
* bound methods: ``__self__`` and ``__func__``;
* instances of a type defined in ``veritas.*`` or in a test module: ``vars()``
  and ``__slots__``, plus the methods and properties of their classes (and the
  same members for such a class reached as a class object);
* list, tuple, set, frozenset and dict (keys and values);
* functions defined in ``veritas.*`` or a test module: every name in
  ``__code__.co_names`` that resolves through ``__globals__``.

Three gap rules close what the last bullet alone misses:

* G1: a name that resolves to a module is entered only through attribute names
  that appear in the same code object's ``co_names`` (``os.system`` enters
  ``os`` through ``system``); repeated for submodules up to a depth cap.
* G2: code objects in ``co_consts`` (lambdas, inner defs, generator
  expressions) are walked too, resolving names against the outer function's
  ``__globals__``.
* G3: imports inside a function body, read from the bytecode with ``dis``.
  ``import a.b`` follows the module; ``from a import x`` follows only ``x``. A
  module that is not installed is recorded in ``missing_modules`` and skipped.

Rules (``Hit.rule``):

* W1: an instance or the class object of SuppressionStore, GitHubPoster or
  GitLabPoster.
* W2: a callable defined in ``veritas.review.tools.write_tools``.
* W3: the module ``write_tools`` or ``veritas.suppression.store``.
* W4: duck typing, only for types defined in ``veritas.*`` or a test module: a
  method named in ``W4_METHODS``.
* W5: an ``httpx.Client``/``AsyncClient`` whose request hooks lack the
  read-only guard.
* X1: ``subprocess`` or ``shutil`` (module or anything defined there), or one
  of the process-spawning ``os`` functions.
* S: a ``Settings`` instance.

Documented exceptions (recorded in ``WalkResult.exceptions``, not walked):

* E1: ``Log.stream`` when it is ``sys.stderr``; any other stream is a hit.
* E2: a ``BaseChatModel`` (the chat model inside ``LLMClient``).

Limits: names are resolved statically, so builtins such as ``open()``,
``getattr`` with a computed name, ``importlib`` and a ``Command(goto=...)``
returned at run time are invisible to it. Attribute names in ``co_names`` that
happen to equal a global name are followed too; that over-approximates and can
only add hits, never hide one.
"""

from __future__ import annotations

import dis
import functools
import importlib
import importlib.util
import os
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from langchain_core.language_models import BaseChatModel

from veritas.config.settings import Settings
from veritas.hosting import github as github_module
from veritas.hosting import gitlab as gitlab_module
from veritas.hosting.github import GitHubPoster
from veritas.hosting.gitlab import GitLabPoster
from veritas.suppression.store import SuppressionStore
from veritas.utils.logging import Log

TESTS_DIR = Path(__file__).resolve().parent

W1_TYPES = (SuppressionStore, GitHubPoster, GitLabPoster)
WRITE_TOOLS_MODULE = "veritas.review.tools.write_tools"
W3_MODULES = frozenset({WRITE_TOOLS_MODULE, "veritas.suppression.store"})
W4_METHODS = (
    "post_comment",
    "post_note",
    "save",
    "upsert_suppression",
    "remove",
    "write_report",
    "persist_last_report_json",
    "update_suppressions",
    "write_text",
    "write_bytes",
)
READ_ONLY_GUARDS = (github_module._reject_writes, gitlab_module._reject_writes)
X1_MODULES = frozenset({"subprocess", "shutil"})
_X1_OS_NAMES = ("system", "popen", "startfile", "posix_spawn", "posix_spawnp")
X1_OS_FUNCTIONS = tuple(
    getattr(os, name)
    for name in dir(os)
    if name in _X1_OS_NAMES or name.startswith(("exec", "spawn"))
    if callable(getattr(os, name))
)
MODULE_DEPTH_CAP = 4

_SCALARS = (str, bytes, bytearray, int, float, complex, bool, type(None), range)


@dataclass(frozen=True)
class Hit:
    rule: str
    path: str
    detail: str


@dataclass
class WalkResult:
    hits: list[Hit] = field(default_factory=list)
    exceptions: list[Hit] = field(default_factory=list)
    missing_modules: list[str] = field(default_factory=list)
    # id -> object for everything visited; holding the object keeps its id unique.
    reached: dict[int, object] = field(default_factory=dict)

    def reaches(self, obj: object) -> bool:
        return self.reached.get(id(obj)) is obj

    def rules(self) -> set[str]:
        return {hit.rule for hit in self.hits}


@functools.lru_cache(maxsize=None)
def is_project_module(name: str | None) -> bool:
    """True for ``veritas`` and its submodules and for modules under tests/."""
    if not name:
        return False
    if name == "veritas" or name.startswith("veritas."):
        return True
    module = sys.modules.get(name)
    file = getattr(module, "__file__", None)
    if not file:
        return False
    return Path(file).resolve().is_relative_to(TESTS_DIR)


def _is_project_class(obj: object) -> bool:
    return isinstance(obj, type) and is_project_module(obj.__module__)


def _module_of(obj: object) -> str | None:
    try:
        name = getattr(obj, "__module__", None)
    except Exception:  # noqa: BLE001 - an exotic __getattr__ is not a module name
        return None
    return name if isinstance(name, str) else None


def check(obj: object) -> tuple[str, str] | None:
    """The rule ``obj`` breaks, as ``(rule, detail)``, or None."""
    if isinstance(obj, types.ModuleType):
        if obj.__name__ in W3_MODULES:
            return "W3", f"module {obj.__name__}"
        if obj.__name__ in X1_MODULES:
            return "X1", f"module {obj.__name__}"
        return None
    if isinstance(obj, W1_TYPES):
        return "W1", f"{type(obj).__name__} instance"
    if isinstance(obj, type) and obj in W1_TYPES:
        return "W1", f"{obj.__name__} class"
    module = _module_of(obj)
    if callable(obj) and module == WRITE_TOOLS_MODULE:
        return "W2", f"{WRITE_TOOLS_MODULE}.{getattr(obj, '__name__', type(obj).__name__)}"
    if callable(obj) and module in X1_MODULES:
        return "X1", f"{module}.{getattr(obj, '__name__', type(obj).__name__)}"
    if any(obj is fn for fn in X1_OS_FUNCTIONS):
        return "X1", f"os.{obj.__name__}"
    if isinstance(obj, (httpx.Client, httpx.AsyncClient)):
        hooks = obj.event_hooks.get("request", [])
        if not any(hook is guard for hook in hooks for guard in READ_ONLY_GUARDS):
            return "W5", f"{type(obj).__name__} without the read-only request hook"
        return None
    if isinstance(obj, Settings):
        return "S", "Settings instance"
    cls = obj if isinstance(obj, type) else type(obj)
    if _is_project_class(cls):
        for name in W4_METHODS:
            if callable(getattr(cls, name, None)):
                return "W4", f"{cls.__qualname__}.{name}"
    return None


def _code_children(code: types.CodeType, globals_: dict, prefix: tuple, opts) -> list:
    """Children of one code object: globals by name, function-body imports (G3)
    and, recursively, nested code objects (G2)."""
    names = code.co_names
    children: list = []
    for name in names:
        if name in globals_:
            children.append((globals_[name], prefix + (f"global:{name}",), names, 0))
    if opts["g3"]:
        children.extend(_import_children(code, globals_, prefix, opts))
    if opts["g2"]:
        for const in code.co_consts:
            if isinstance(const, types.CodeType):
                label = f"<code {const.co_qualname}>"
                children.extend(_code_children(const, globals_, prefix + (label,), opts))
    return children


def _import(name: str, result: WalkResult):
    try:
        return importlib.import_module(name)
    except ImportError:
        if name not in result.missing_modules:
            result.missing_modules.append(name)
        return None


def _import_children(code: types.CodeType, globals_: dict, prefix: tuple, opts) -> list:
    """G3: ``import``/``from ... import`` statements inside a function body.

    CPython compiles them to ``LOAD_CONST level; LOAD_CONST fromlist;
    IMPORT_NAME name`` followed by one ``IMPORT_FROM`` per imported name.
    """
    result: WalkResult = opts["result"]
    names = code.co_names
    children: list = []
    instructions = list(dis.get_instructions(code))
    base = None
    base_name = ""
    for index, ins in enumerate(instructions):
        if ins.opname == "IMPORT_NAME":
            level = instructions[index - 2].argval if index >= 2 else 0
            fromlist = instructions[index - 1].argval if index >= 1 else None
            absolute = ins.argval
            if level:
                package = globals_.get("__package__") or ""
                absolute = importlib.util.resolve_name("." * level + ins.argval, package)
            module = _import(absolute, result)
            if module is None:
                base = None
                continue
            if fromlist:
                # "from a import x": only x is followed, by IMPORT_FROM below.
                base, base_name = module, absolute
            else:
                # "import a.b": the statement binds the top package; both it
                # and the named module are entered through this code's names.
                top_name = absolute.split(".")[0]
                top = sys.modules[top_name]
                base, base_name = top, top_name
                children.append((module, prefix + (f"import {absolute}",), names, 0))
                if top is not module:
                    children.append((top, prefix + (f"import {top_name}",), names, 0))
        elif ins.opname == "IMPORT_FROM" and base is not None:
            attr = ins.argval
            if hasattr(base, attr):
                value = getattr(base, attr)
            else:
                value = _import(f"{base_name}.{attr}", result)
                if value is None:
                    continue
            children.append((value, prefix + (f"from {base_name} import {attr}",), names, 0))
    return children


def _class_members(cls: type, path: tuple) -> list:
    """Methods and properties defined directly on ``cls``."""
    children: list = []
    for name, member in list(vars(cls).items()):
        label = f"{cls.__qualname__}.{name}"
        if isinstance(member, property):
            for part in ("fget", "fset", "fdel"):
                accessor = getattr(member, part)
                if accessor is not None:
                    children.append((accessor, path + (f"{label}.{part}",), None, 0))
        elif isinstance(member, (staticmethod, classmethod)):
            children.append((member.__func__, path + (label,), None, 0))
        elif isinstance(member, (types.FunctionType, functools.partial)):
            children.append((member, path + (label,), None, 0))
    return children


def _instance_children(obj: object, path: tuple, result: WalkResult) -> list:
    cls = type(obj)
    children: list = []
    attrs: dict[str, object] = {}
    if hasattr(obj, "__dict__") and isinstance(vars(obj), dict):
        attrs.update(vars(obj))
    for klass in cls.__mro__:
        slots = klass.__dict__.get("__slots__", ())
        if isinstance(slots, str):
            slots = (slots,)
        for slot in slots:
            if slot in ("__dict__", "__weakref__") or slot in attrs:
                continue
            try:
                attrs[slot] = getattr(obj, slot)
            except AttributeError:
                continue
    for name, value in attrs.items():
        if isinstance(obj, Log) and name == "stream":
            # E1: the logger may write, but only to stderr.
            label = path + (f".{name}",)
            if value is sys.stderr:
                result.exceptions.append(Hit("E1", " -> ".join(label), "Log.stream is sys.stderr"))
            else:
                result.hits.append(Hit("E1", " -> ".join(label), "Log.stream is not sys.stderr"))
            continue
        children.append((value, path + (f".{name}",), None, 0))
    for klass in cls.__mro__:
        if _is_project_class(klass):
            children.extend(_class_members(klass, path))
    return children


def _children(obj: object, path: tuple, opts) -> list:
    result: WalkResult = opts["result"]
    if isinstance(obj, types.FunctionType):
        children: list = []
        code = obj.__code__
        for name, cell in zip(code.co_freevars, obj.__closure__ or ()):
            try:
                value = cell.cell_contents
            except ValueError:  # empty cell
                continue
            children.append((value, path + (f"closure[{name}]",), None, 0))
        for index, value in enumerate(obj.__defaults__ or ()):
            children.append((value, path + (f"default[{index}]",), None, 0))
        for name, value in (obj.__kwdefaults__ or {}).items():
            children.append((value, path + (f"kwdefault[{name}]",), None, 0))
        if is_project_module(_module_of(obj)):
            children.extend(_code_children(code, obj.__globals__, path, opts))
        return children
    if isinstance(obj, functools.partial):
        children = [(obj.func, path + ("partial.func",), None, 0)]
        children += [(v, path + (f"partial.args[{i}]",), None, 0) for i, v in enumerate(obj.args)]
        children += [(v, path + (f"partial.keywords[{k}]",), None, 0) for k, v in obj.keywords.items()]
        return children
    if isinstance(obj, types.MethodType):
        return [(obj.__self__, path + ("__self__",), None, 0), (obj.__func__, path + ("__func__",), None, 0)]
    if isinstance(obj, dict):
        children = []
        for key, value in obj.items():
            children.append((key, path + (f"key({key!r})",), None, 0))
            children.append((value, path + (f"[{key!r}]",), None, 0))
        return children
    if isinstance(obj, tuple) and hasattr(type(obj), "_fields"):  # NamedTuple
        return [(v, path + (f".{n}",), None, 0) for n, v in zip(type(obj)._fields, obj)]
    if isinstance(obj, (list, tuple)):
        return [(v, path + (f"[{i}]",), None, 0) for i, v in enumerate(obj)]
    if isinstance(obj, (set, frozenset)):
        return [(v, path + (f"{{{v!r}}}",), None, 0) for v in sorted(obj, key=repr)]
    if _is_project_class(obj):
        children = []
        for klass in obj.__mro__:
            if _is_project_class(klass):
                children.extend(_class_members(klass, path))
        return children
    if _is_project_class(type(obj)):
        return _instance_children(obj, path, result)
    return []


def walk(root: object, label: str = "root", *, g1: bool = True, g2: bool = True, g3: bool = True) -> WalkResult:
    """Walk everything reachable from ``root``; every hit carries its path."""
    result = WalkResult()
    opts = {"g1": g1, "g2": g2, "g3": g3, "result": result}
    visited: set[int] = set()
    entered: set[tuple[int, str]] = set()  # (module id, attribute) pairs G1 followed
    stack: list = [(root, (label,), None, 0)]
    while stack:
        obj, path, names, depth = stack.pop()
        if isinstance(obj, _SCALARS):
            continue
        is_module = isinstance(obj, types.ModuleType)
        if id(obj) not in visited:
            visited.add(id(obj))
            result.reached[id(obj)] = obj
            broken = check(obj)
            if broken is not None:
                result.hits.append(Hit(broken[0], " -> ".join(path), broken[1]))
                continue
            if isinstance(obj, BaseChatModel):
                result.exceptions.append(Hit("E2", " -> ".join(path), type(obj).__name__))
                continue
            if not is_module:
                children = _children(obj, path, opts)
                stack.extend(reversed(children))
                continue
        elif not is_module or check(obj) is not None:
            continue
        # G1: a module is entered only through the names its code used.
        if not g1 or not names or depth >= MODULE_DEPTH_CAP:
            continue
        children = []
        for attr in names:
            if (id(obj), attr) in entered or not hasattr(obj, attr):
                continue
            entered.add((id(obj), attr))
            children.append((getattr(obj, attr), path + (f".{attr}",), names, depth + 1))
        stack.extend(reversed(children))
    return result
