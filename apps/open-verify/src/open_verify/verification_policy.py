"""Recognize common existing-test launchers, not a sandbox or proof of independence."""

import ast
import shlex
from pathlib import PurePath


def existing_test_command(argv: list[str]) -> bool:
    """Catch direct launchers and conventional shell/Python wrappers before execution.

    Arbitrary programs can invoke tests internally; discovery and explicit case
    classification still matter. This guard prevents the common pytest wrapper
    from being reported as an independent live journey.
    """
    runners = {"pytest", "py.test", "unittest", "vitest", "jest", "mocha", "tox", "nox"}
    words = list(argv)
    for argument in argv:
        try:
            words.extend(shlex.split(argument))
        except ValueError:
            words.append(argument)
    names = [PurePath(word.replace("\\", "/")).name.removesuffix(".exe").strip(";&|") for word in words]
    if any(name in runners for name in names):
        return True
    for index, name in enumerate(names):
        tail = names[index + 1:]
        if name in {"npm", "pnpm", "yarn", "bun"} and any(
            word == "test" or word.startswith("test:") for word in tail
        ):
            return True
        if name == "playwright" and "test" in tail:
            return True
        if name in {"cargo", "go", "dotnet", "gradle", "mvn"} and "test" in tail:
            return True
    # A Python inline wrapper is still an existing-test invocation.
    if "-c" in argv:
        index = argv.index("-c")
        if index + 1 < len(argv):
            try:
                tree = ast.parse(argv[index + 1])
            except SyntaxError:
                return False
            for node in ast.walk(tree):
                if isinstance(node, ast.Import) and any(
                    alias.name.split(".")[0] in runners for alias in node.names
                ):
                    return True
                if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in runners:
                    return True
    return False
