"""Discovery of the monorepo's packages and the layer each one belongs to.

Shared by the boundary tests. Everything here is static analysis -- the source
tree and the pyproject files are the input, never a live import -- so a
violation is caught even when the offending import sits behind a branch that
never executes in tests.
"""

import ast
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Layers, innermost first. A package may import its own layer and any layer
#: listed before it -- except adapters and apps, which get extra rules below.
DOMAIN = "domain"
ENGINE = "engine"
PORTS = "ports"
RUNTIME = "runtime"
ADAPTER = "adapter"
APP = "app"

#: The layers that make up the dependency-free core of the system.
CORE_LAYERS = frozenset({DOMAIN, ENGINE, PORTS, RUNTIME})

#: Adapters are grouped by capability, so a distribution sits two levels under
#: here: `packages/adapters/<capability>/<vendor>`.
ADAPTERS_ROOT = REPO_ROOT / "packages" / "adapters"

#: Where the capability names come from. `Capabilities` has one field per
#: capability, which makes it the only list that cannot drift -- adding a
#: seventh capability there is what permits a seventh adapter directory. Read
#: with `ast` rather than imported, so this module stays static analysis.
CAPABILITIES_SOURCE = REPO_ROOT / "packages/runtime/src/engine/runtime/capabilities.py"

#: Acceptance criterion: these two must not pull in any third-party code.
NO_THIRD_PARTY_LAYERS = frozenset({DOMAIN, ENGINE})

#: Which `engine.*` module prefixes each layer is allowed to import.
#: Adapters additionally import their own module; apps may import anything.
ALLOWED_ENGINE_PREFIXES: dict[str, tuple[str, ...]] = {
    DOMAIN: ("engine.domain",),
    ENGINE: ("engine.domain", "engine.core"),
    PORTS: ("engine.domain", "engine.ports"),
    # `engine.graph_runtime` is here because it is a contract rather than an
    # implementation: a runtime-layer package may be written against it, which
    # is what `engine.graph_runtime_langgraph` is. Nothing in it names a vendor,
    # so depending on it is not depending on a concrete anything.
    RUNTIME: (
        "engine.domain",
        "engine.core",
        "engine.ports",
        "engine.runtime",
        "engine.graph_runtime",
        "engine.orchestrator",
    ),
    ADAPTER: ("engine.domain", "engine.core", "engine.ports", "engine.runtime"),
    APP: ("engine.domain", "engine.core", "engine.ports", "engine.runtime", "engine.adapters"),
}


@dataclass(frozen=True)
class Package:
    """One distribution in the workspace."""

    dist_name: str
    layer: str
    root: Path
    modules: tuple[str, ...]
    declared_dependencies: tuple[str, ...]

    @property
    def src(self) -> Path:
        return self.root / "src"

    @property
    def rel_root(self) -> str:
        return str(self.root.relative_to(REPO_ROOT))

    def source_files(self) -> list[Path]:
        return sorted(self.src.rglob("*.py"))

    def __str__(self) -> str:  # pytest parametrisation ids
        return self.rel_root


def _layer_for(root: Path) -> str:
    rel = root.relative_to(REPO_ROOT).parts
    match rel:
        case ("packages", "adapters", *_):
            return ADAPTER
        case ("apps", *_):
            return APP
        case ("packages", "domain"):
            return DOMAIN
        case ("packages", "engine"):
            return ENGINE
        case ("packages", "scoper"):
            return RUNTIME
        case ("packages", "graph_runtime"):
            return RUNTIME
        case ("packages", "graph_runtime_langgraph"):
            return RUNTIME
        case ("packages", "orchestrator"):
            return RUNTIME
        case ("packages", "ports"):
            return PORTS
        case ("packages", "runtime"):
            return RUNTIME
        case _:
            raise AssertionError(f"unclassified package at {root}")


def _modules_in(src: Path) -> tuple[str, ...]:
    """Dotted names of every package shipped from this src root."""
    return tuple(
        sorted(
            ".".join(init.parent.relative_to(src).parts)
            for init in src.rglob("__init__.py")
        )
    )


def capability_names() -> tuple[str, ...]:
    """The capability slots, in declaration order, read off `Capabilities`."""
    tree = ast.parse(CAPABILITIES_SOURCE.read_text(), filename=str(CAPABILITIES_SOURCE))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Capabilities":
            return tuple(
                statement.target.id
                for statement in node.body
                if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name)
            )
    raise AssertionError(f"no Capabilities class in {CAPABILITIES_SOURCE}")


CAPABILITIES = capability_names()


def adapter_path_parts(package: Package) -> tuple[str, ...]:
    """An adapter's position under `packages/adapters`, as directory names.

    Returned whole rather than unpacked so a package at the wrong depth reaches
    the assertion that explains it instead of raising here.
    """
    return package.root.relative_to(ADAPTERS_ROOT).parts


def discover_packages() -> tuple[Package, ...]:
    """Every workspace member, found the same way uv finds them."""
    roots = [
        *(REPO_ROOT / "packages").glob("*/pyproject.toml"),
        # Two levels: the capability directory groups, the vendor directory ships.
        *ADAPTERS_ROOT.glob("*/*/pyproject.toml"),
        *(REPO_ROOT / "apps").glob("*/pyproject.toml"),
    ]
    packages = []
    for pyproject in roots:
        root = pyproject.parent
        config = tomllib.loads(pyproject.read_text())
        project = config["project"]
        packages.append(
            Package(
                dist_name=project["name"],
                layer=_layer_for(root),
                root=root,
                modules=_modules_in(root / "src"),
                declared_dependencies=tuple(project.get("dependencies", ())),
            )
        )
    return tuple(sorted(packages, key=lambda p: p.rel_root))


PACKAGES = discover_packages()


def imported_modules(path: Path) -> set[str]:
    """Every module name imported by a source file, absolute form.

    Relative imports are resolved against the file's own package so that a
    sneaky `from ...adapters.source_control.github import X` cannot slip past the
    check.
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                found.add(_resolve_relative(path, node))
            elif node.module:
                found.add(node.module)
    return found


def _resolve_relative(path: Path, node: ast.ImportFrom) -> str:
    """Turn `from ..x import y` inside a file into a dotted absolute name."""
    src = _src_root_for(path)
    parts = list(path.relative_to(src).parts)
    parts[-1] = parts[-1].removesuffix(".py")
    # Drop the file's own name to get its containing package: `__init__.py` *is*
    # its package, and any other module lives one level inside one.
    parts.pop()
    # `level` 1 means the containing package; each extra dot climbs one more.
    ascend = node.level - 1
    base = parts[: len(parts) - ascend] if ascend <= len(parts) else []
    return ".".join([*base, *([node.module] if node.module else [])])


def _src_root_for(path: Path) -> Path:
    for parent in path.parents:
        if parent.name == "src":
            return parent
    raise AssertionError(f"{path} is not under a src/ root")


def is_stdlib(module: str) -> bool:
    return module.split(".")[0] in sys.stdlib_module_names


def is_first_party(module: str) -> bool:
    return module == "engine" or module.startswith("engine.")


def third_party_imports(package: Package) -> dict[Path, set[str]]:
    """Imports that are neither stdlib nor part of this workspace."""
    offenders: dict[Path, set[str]] = {}
    for file in package.source_files():
        external = {
            module
            for module in imported_modules(file)
            if not is_stdlib(module) and not is_first_party(module)
        }
        if external:
            offenders[file] = external
    return offenders


def engine_imports(package: Package) -> dict[Path, set[str]]:
    """First-party imports, per file."""
    return {
        file: {m for m in imported_modules(file) if is_first_party(m)}
        for file in package.source_files()
    }


def by_layer(layer: str) -> tuple[Package, ...]:
    return tuple(p for p in PACKAGES if p.layer == layer)


def core_packages() -> tuple[Package, ...]:
    return tuple(p for p in PACKAGES if p.layer in CORE_LAYERS)
