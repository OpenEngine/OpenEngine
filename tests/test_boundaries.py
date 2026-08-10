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
    CAPABILITIES,
    CORE_LAYERS,
    NO_THIRD_PARTY_LAYERS,
    PACKAGES,
    Package,
    adapter_path_parts,
    by_layer,
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


# --- adapters are filed under the capability they implement ----------------


@pytest.mark.parametrize("package", by_layer(ADAPTER), ids=_ids(by_layer(ADAPTER)))
def test_adapter_sits_under_the_capability_it_implements(package: Package) -> None:
    """`packages/adapters/<capability>/<vendor>`: what first, who second.

    A package named for a capability but holding one vendor's code quietly
    claims the whole capability -- the Buzz adapter is `communications/buzz`,
    not `communications`, because Slack has to be able to sit beside it without
    either one being privileged.
    """
    parts = adapter_path_parts(package)
    assert len(parts) == 2, (
        f"{package.rel_root} is at the wrong depth; adapters live at "
        "packages/adapters/<capability>/<vendor>"
    )

    capability, vendor = parts
    assert capability in CAPABILITIES, (
        f"{package.rel_root}: '{capability}' is not a capability. Expected one of "
        f"{sorted(CAPABILITIES)} -- the fields on `Capabilities`, which is the "
        "list a seventh capability has to be added to first."
    )
    assert vendor != capability, (
        f"{package.rel_root}: an implementation named after its own capability "
        "claims more scope than it has; name it for who provides it"
    )


@pytest.mark.parametrize("package", by_layer(ADAPTER), ids=_ids(by_layer(ADAPTER)))
def test_adapter_module_path_mirrors_its_directory(package: Package) -> None:
    """The import path is the directory path, so neither can be read alone."""
    expected = "engine.adapters." + ".".join(adapter_path_parts(package))
    assert package.modules == (expected,), (
        f"{package.rel_root} ships {list(package.modules)}, expected [{expected!r}]"
    )


def test_every_capability_groups_at_least_one_adapter() -> None:
    """The mirror of `Capabilities` being total: a capability with no
    implementation filed under it is a hole in the composition root."""
    grouped = {adapter_path_parts(p)[0] for p in by_layer(ADAPTER)}
    assert grouped == set(CAPABILITIES), (
        f"capabilities with no adapter: {sorted(set(CAPABILITIES) - grouped)}; "
        f"adapter directories naming no capability: {sorted(grouped - set(CAPABILITIES))}"
    )


# --- adapters are siblings, not a stack ------------------------------------


@pytest.mark.parametrize("package", by_layer(ADAPTER), ids=_ids(by_layer(ADAPTER)))
def test_adapters_do_not_import_each_other(package: Package) -> None:
    """Two adapters that need each other belong behind one port.

    Grouping by capability puts vendors next to each other, which makes this the
    easy mistake to make: `communications/slack` importing `communications/buzz`
    turns one implementation into the other's base class, and the port stops
    being the only thing they have in common.
    """
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


def test_apps_actually_wire_adapters() -> None:
    """If no app names an adapter, the composition root is not composing."""
    for package in by_layer(APP):
        imported = {m for modules in engine_imports(package).values() for m in modules}
        assert any(m.startswith("engine.adapters") for m in imported), (
            f"{package.dist_name} imports no adapters; it is not a composition root"
        )
