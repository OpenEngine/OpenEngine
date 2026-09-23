"""Release wheels must contain the built client; editable development need not."""
from pathlib import Path
from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        if self.target_name == "wheel" and version != "editable":
            if not (Path(self.root) / "src/engine/apps/web/static/index.html").is_file():
                raise RuntimeError("Build the frontend with npm run build before building engine-web")
