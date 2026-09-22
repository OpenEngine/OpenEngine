"""Bundle runtime resources in release wheels without requiring a UI build for development."""

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        if version == "standard":
            build_data["force_include"].update({
                "dist": "engine/apps/web/static",
                "../../workflows": "engine/apps/web/default_workflows",
            })
