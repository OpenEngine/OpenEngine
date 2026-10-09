"""GitHub-hosted evidence; explicitly installed by an OE deployment."""

import argparse
import asyncio
import sys
from pathlib import Path
from urllib.parse import quote

from engine.adapters.source_control.github import GitHubSourceControl
from engine.adapters.source_control.github.transports import (
    GitHubCliTransport,
    GitHubTransportError,
)
from engine.domain import WorkspaceId
from engine.runtime.change_requests import change_request
from engine.runtime.verification import VerificationArtifact, publish_verification


class GitHubVerificationUploader:
    """Store immutable, content-addressed evidence in a prerelease per PR head.

    Uses the host's existing gh authentication. It never uploads diagnostics,
    browser profiles, or any file not validated by the bundle consumer.
    """

    def __init__(self, transport=None):
        self.transport = transport or GitHubCliTransport()
        self._releases = {}

    async def upload(
        self, pr_url: str, head: str, artifact: VerificationArtifact
    ) -> str:
        target = change_request(pr_url)
        if target is None or target.kind != "pull" or target.host != "github.com":
            raise ValueError(
                "GitHub verification uploads require a github.com pull request"
            )
        if self.transport.host != "github.com":
            raise ValueError("Upload credentials must target github.com")
        repository = target.path
        tag = f"open-verify/pr-{target.number}/{head}"
        prefix = f"/repos/{repository}/releases"
        key = (repository, tag)
        if key not in self._releases:
            try:
                release = await self.transport.request(
                    "GET", f"{prefix}/tags/{quote(tag, safe='')}"
                )
            except GitHubTransportError:
                try:
                    release = await self.transport.request(
                        "POST",
                        prefix,
                        json={
                            "tag_name": tag,
                            "target_commitish": head,
                            "name": f"Open Verify: PR #{target.number} ({head[:12]})",
                            "body": "End-to-end test evidence produced by Open Verify.",
                            "prerelease": True,
                            "make_latest": "false",
                        },
                    )
                except GitHubTransportError:
                    # Another worker may have created the same release.
                    release = await self.transport.request(
                        "GET", f"{prefix}/tags/{quote(tag, safe='')}"
                    )
            if type(release.get("id")) is not int or release.get("draft"):
                raise ValueError("GitHub returned an invalid evidence release")
            self._releases[key] = release["id"]
        release_id = self._releases[key]
        # Paginate rather than missing existing assets and creating duplicates.
        page = 1
        while True:
            assets = await self.transport.request(
                "GET",
                f"{prefix}/{release_id}/assets",
                params={"per_page": 100, "page": page},
            )
            for asset in assets:
                if asset.get("name") == artifact.name:
                    return self._url(asset, artifact)
            if len(assets) < 100:
                break
            page += 1
        asset = await self.transport.upload_release_asset(
            repository,
            release_id,
            artifact.name,
            artifact.data,
            artifact.media_type,
        )
        return self._url(asset, artifact)

    @staticmethod
    def _url(asset, artifact):
        if asset.get("size") != len(artifact.data) or asset.get("state") != "uploaded":
            raise ValueError("Uploaded verification asset failed size/state validation")
        if (
            asset.get("digest")
            and asset["digest"] != "sha256:" + artifact.name.split(".")[0]
        ):
            raise ValueError("Uploaded verification asset digest mismatch")
        url = asset.get("browser_download_url")
        if not isinstance(url, str) or not url.startswith("https://github.com/"):
            raise ValueError("GitHub returned an invalid evidence download URL")
        return url


class _LocalWorkspace:
    def __init__(self, root: Path):
        self.root = root

    async def root_path(self, workspace_id: WorkspaceId) -> str:
        return str(self.root)


def main(argv=None) -> int:
    cli = argparse.ArgumentParser(
        description="Publish an Open Verify bundle as GitHub release assets and a PR comment."
    )
    cli.add_argument("--manifest", required=True, type=Path, help="Run's manifest.json")
    cli.add_argument("--pr", required=True, help="Target github.com pull request URL")
    cli.add_argument(
        "--project",
        type=Path,
        default=Path.cwd(),
        help="Checkout with the PR repository as origin",
    )
    args = cli.parse_args(argv)
    target = change_request(args.pr)
    if target is None or target.host != "github.com" or target.kind != "pull":
        cli.error("--pr must name a github.com pull request")
    project = args.project.resolve()
    if not project.is_dir() or not args.manifest.is_file():
        cli.error("--project must be a directory and --manifest an existing file")
    transport = GitHubCliTransport(host="github.com")
    source = GitHubSourceControl(
        token="",
        transport=transport,
        workspace_provider=_LocalWorkspace(project),
    )
    try:
        result = asyncio.run(
            publish_verification(
                args.manifest.absolute(),
                workspace_id=WorkspaceId("local-verification"),
                pr_url=args.pr,
                source_control=source,
                uploader=GitHubVerificationUploader(transport),
            )
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Verification publication failed: {exc}", file=sys.stderr)
        return 2
    print(f"Verification: {result['status']}")
    print(
        f"PR comment: {result['comment_url']}"
        if result["comment_url"]
        else "No artifacts to publish."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
