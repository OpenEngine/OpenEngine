"""The dependency direction, enforced.

Each test maps to a line in Ticket 1's acceptance criteria. These are static
checks over the source tree -- they fail on a bad `import` the moment it is
written, whether or not any code path reaches it.

    domain
      ^
    engine

    ports
      ^
    runtime
      ^
    adapters

    apps compose everything together
"""

import pytest

from layout import (
    ADAPTER,
    ALLOWED_ENGINE_PREFIXES,
    APP,
    CORE_LAYERS,
    NO_THIRD_PARTY_LAYERS,
    PACKAGES,
    REPO_ROOT,
    Package,
    by_layer,
    code_without_docstrings,
    core_packages,
    engine_imports,
    third_party_imports,
)

WORKSPACE_DISTS = {p.dist_name for p in PACKAGES}


def _ids(packages: tuple[Package, ...]) -> list[str]:
    return [p.rel_root for p in packages]


# --- domain and engine are dependency-free ---------------------------------


@pytest.mark.parametrize(
    "package",
    [p for p in PACKAGES if p.layer in NO_THIRD_PARTY_LAYERS],
    ids=_ids(tuple(p for p in PACKAGES if p.layer in NO_THIRD_PARTY_LAYERS)),
)
def test_declares_no_third_party_dependencies(package: Package) -> None:
    """`domain` and `engine` may depend on workspace siblings and nothing else."""
    external = [
        dep for dep in package.declared_dependencies if dep.split()[0] not in WORKSPACE_DISTS
    ]
    assert not external, (
        f"{package.dist_name} declares third-party dependencies {external}; "
        "this layer must stay installable on a bare interpreter"
    )


@pytest.mark.parametrize(
    "package",
    [p for p in PACKAGES if p.layer in NO_THIRD_PARTY_LAYERS],
    ids=_ids(tuple(p for p in PACKAGES if p.layer in NO_THIRD_PARTY_LAYERS)),
)
def test_imports_no_third_party_modules(package: Package) -> None:
    """Belt and braces: an undeclared import is still an import."""
    offenders = third_party_imports(package)
    assert not offenders, (
        f"{package.dist_name} imports non-stdlib, non-workspace modules: "
        + ", ".join(f"{f.name} -> {sorted(m)}" for f, m in offenders.items())
    )


# --- core knows nothing about the outside world ----------------------------


@pytest.mark.parametrize("package", core_packages(), ids=_ids(core_packages()))
def test_core_does_not_import_adapters(package: Package) -> None:
    """The whole point: core code must not name a concrete implementation."""
    offenders = {
        file: sorted(m for m in modules if m.startswith("engine.adapters"))
        for file, modules in engine_imports(package).items()
    }
    offenders = {f: m for f, m in offenders.items() if m}
    assert not offenders, (
        f"{package.dist_name} imports adapters: "
        + ", ".join(f"{f.name} -> {m}" for f, m in offenders.items())
        + " -- emit a command instead"
    )


@pytest.mark.parametrize("package", core_packages(), ids=_ids(core_packages()))
def test_core_does_not_import_apps(package: Package) -> None:
    """`apps` is the composition root; nothing may depend back onto it."""
    offenders = {
        file: sorted(m for m in modules if m.startswith("engine.apps"))
        for file, modules in engine_imports(package).items()
    }
    offenders = {f: m for f, m in offenders.items() if m}
    assert not offenders, f"{package.dist_name} imports composition roots: {offenders}"


@pytest.mark.parametrize("package", core_packages(), ids=_ids(core_packages()))
def test_core_declares_no_adapter_dependencies(package: Package) -> None:
    """The import rule again, at the packaging level."""
    offenders = [
        dep for dep in package.declared_dependencies if dep.startswith("engine-adapter")
    ]
    assert not offenders, f"{package.dist_name} declares adapter dependencies {offenders}"


# --- every package imports only from layers below it -----------------------


@pytest.mark.parametrize("package", PACKAGES, ids=_ids(PACKAGES))
def test_imports_stay_within_allowed_layers(package: Package) -> None:
    """The arrows in the README, checked."""
    allowed = list(ALLOWED_ENGINE_PREFIXES[package.layer])
    # A package may always import itself.
    allowed.extend(package.modules)

    violations: list[str] = []
    for file, modules in engine_imports(package).items():
        for module in sorted(modules):
            if module == "engine":
                continue  # the namespace root itself carries no code
            if not any(module == a or module.startswith(f"{a}.") for a in allowed):
                violations.append(f"{file.name} -> {module}")

    assert not violations, (
        f"{package.dist_name} ({package.layer} layer) may import {sorted(set(allowed))}; "
        f"found {violations}"
    )


# --- adapters are siblings, not a stack ------------------------------------


@pytest.mark.parametrize("package", by_layer(ADAPTER), ids=_ids(by_layer(ADAPTER)))
def test_adapters_do_not_import_each_other(package: Package) -> None:
    """Two adapters that need each other belong behind one port."""
    own = set(package.modules)
    violations: list[str] = []
    for file, modules in engine_imports(package).items():
        for module in sorted(modules):
            if not module.startswith("engine.adapters"):
                continue
            if not any(module == o or module.startswith(f"{o}.") for o in own):
                violations.append(f"{file.name} -> {module}")
    assert not violations, f"{package.dist_name} reaches into another adapter: {violations}"


@pytest.mark.parametrize("package", by_layer(ADAPTER), ids=_ids(by_layer(ADAPTER)))
def test_adapters_may_depend_on_core(package: Package) -> None:
    """The permitted direction, asserted positively so it stays exercised."""
    core_dists = {p.dist_name for p in PACKAGES if p.layer in CORE_LAYERS}
    declared = {dep.split()[0] for dep in package.declared_dependencies}
    assert declared & core_dists, (
        f"{package.dist_name} declares no core dependency ({sorted(declared)}); "
        "an adapter with nothing to implement is dead code"
    )


# --- apps are the composition root -----------------------------------------


def test_only_apps_depend_on_adapters() -> None:
    """Adapters may be named by the composition root and nowhere else."""
    adapter_dists = {p.dist_name for p in by_layer(ADAPTER)}
    for package in PACKAGES:
        if package.layer == APP:
            continue
        declared = {dep.split()[0] for dep in package.declared_dependencies}
        assert not (declared & adapter_dists), (
            f"{package.dist_name} depends on {sorted(declared & adapter_dists)}; "
            "only apps/ may wire concrete implementations"
        )


@pytest.mark.parametrize("package", by_layer(APP), ids=_ids(by_layer(APP)))
def test_apps_do_not_depend_on_each_other(package: Package) -> None:
    """Two deployables that import each other are one deployable."""
    other_apps = {p.dist_name for p in by_layer(APP)} - {package.dist_name}
    declared = {dep.split()[0] for dep in package.declared_dependencies}
    assert not (declared & other_apps), (
        f"{package.dist_name} depends on sibling app {sorted(declared & other_apps)}"
    )


@pytest.mark.parametrize("package", by_layer(APP), ids=_ids(by_layer(APP)))
def test_apps_choose_implementations_somehow(package: Package) -> None:
    """An app must decide which implementations run -- one way or the other.

    Two legitimate shapes, and the difference is whose composition root it is:

    - Import adapters directly. Correct for a deployable we operate.
    - Resolve them by name from installed plugins. Correct for a surface shipped
      to consumers, where welding in a vendor would force a fork to swap it.

    What is *not* legitimate is an app that does neither, which would mean
    nothing anywhere selects an implementation.
    """
    imported = {m for modules in engine_imports(package).values() for m in modules}
    imports_adapters = any(m.startswith("engine.adapters") for m in imported)
    uses_registry = any(
        "resolve_agent_runner" in text or "load_agent_runner" in text
        for text in (f.read_text() for f in package.source_files())
    )
    assert imports_adapters or uses_registry, (
        f"{package.dist_name} neither imports an adapter nor resolves one; "
        "no implementation is ever chosen"
    )


# --- the shipped surface stays provider-agnostic ---------------------------


def test_shipped_surface_declares_no_required_adapter() -> None:
    """`engine-web` and the control server must install without a vendor.

    They are what a consumer deploys. A required adapter dependency would decide
    the backend for them. Optional extras are fine -- those are opt-in bundles,
    which is why this reads `dependencies` and not `optional-dependencies`.
    """
    shipped = {"engine-web", "engine-control-server"}
    for package in PACKAGES:
        if package.dist_name not in shipped:
            continue
        offenders = [
            dep for dep in package.declared_dependencies if dep.startswith("engine-adapter")
        ]
        assert not offenders, (
            f"{package.dist_name} requires {offenders}; a consumer could not "
            "choose their own backend without uninstalling ours"
        )


def test_registry_names_no_adapter() -> None:
    """The plugin seam must stay data-driven.

    `engine.runtime.registry` loads adapters at runtime, which static analysis
    cannot see -- so the AST checks above pass whatever it does. This is the one
    place that matters, checked directly: if a vendor name appears here, the
    'resolved by name' claim is a fiction.
    """
    registry = REPO_ROOT / "packages/runtime/src/engine/runtime/registry.py"
    # Docstrings are exempt: the module documents the entry-point format with a
    # worked example, which is prose about a vendor rather than a dependency on
    # one. What matters is that no executable line names one.
    code = code_without_docstrings(registry)
    for vendor in ("engine.adapters", "anthropic", "openai", "codex", "claude", "strands"):
        assert vendor not in code, (
            f"registry.py's code mentions {vendor!r}; "
            "backend selection must be data, not code"
        )
