"""A release wheel must contain the frontend built by CI."""
from pathlib import Path
from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        if self.target_name == "wheel" and version != "editable":
            assets = Path(self.root) / "dist"
            if not (assets / "index.html").is_file():
                raise RuntimeError("Build apps/web/dist before building the release wheel")
            build_data["force_include"][str(assets)] = "engine/apps/web/static"
