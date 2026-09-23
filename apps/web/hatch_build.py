"""Require built frontend assets in wheels, but allow editable development."""

from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        if self.target_name == "wheel" and version != "editable":
            index = Path(self.root) / "src/engine/apps/web/static/index.html"
            if not index.is_file():
                raise RuntimeError(
                    "Build the frontend with npm --prefix apps/web run build "
                    "before building engine-web"
                )
