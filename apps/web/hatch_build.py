"""Include the production client in wheels without requiring Node for uv sync."""

from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class WebAssetsHook(BuildHookInterface):
    def initialize(self, version, build_data):
        if version == "editable":
            return
        dist = Path(self.root) / "dist"
        if not (dist / "index.html").is_file():
            raise ValueError("Build the web client first: npm --prefix apps/web run build")
        build_data["force_include"][str(dist)] = "engine/apps/web/static"
