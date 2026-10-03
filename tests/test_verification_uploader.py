import asyncio
from unittest.mock import AsyncMock

import pytest
from engine.adapters.source_control.github.transports import (
    GitHubCliTransport,
    GitHubTransportError,
)
from engine.adapters.source_control.github.verification import (
    GitHubVerificationUploader,
)
from engine.runtime.verification import VerificationArtifact

PR = "https://github.com/owner/repo/pull/42"
HEAD = "a" * 40


def test_creates_prerelease_and_uploads_immutable_evidence():
    transport = GitHubCliTransport()
    artifact = VerificationArtifact(
        "screen.png", "screenshot", "C1", "image/png", b"image"
    )
    asset = {
        "name": artifact.name,
        "size": 5,
        "state": "uploaded",
        "browser_download_url": "https://github.com/owner/repo/releases/download/evidence/screen.png",
    }
    transport.request = AsyncMock(
        side_effect=[
            GitHubTransportError("not found"),
            {"id": 12, "draft": False},
            [],
            [asset],
        ]
    )
    transport.upload_release_asset = AsyncMock(return_value=asset)
    uploader = GitHubVerificationUploader(transport)

    async def run():
        first = await uploader.upload(PR, HEAD, artifact)
        assert first == await uploader.upload(PR, HEAD, artifact)

    asyncio.run(run())
    create = transport.request.call_args_list[1]
    assert create.kwargs["json"]["prerelease"] is True
    assert create.kwargs["json"]["make_latest"] == "false"
    assert create.kwargs["json"]["target_commitish"] == HEAD
    transport.upload_release_asset.assert_awaited_once_with(
        "owner/repo", 12, artifact.name, b"image", "image/png"
    )


def test_cli_upload_sends_bytes_as_stdin_and_never_in_command():
    transport = GitHubCliTransport()
    transport._run = AsyncMock(return_value=b'{"id":1}')
    asyncio.run(
        transport.upload_release_asset(
            "owner/repo", 12, "asset.png", b"private-content", "image/png"
        )
    )
    call = transport._run.call_args
    assert call.kwargs == {"input_bytes": b"private-content"}
    assert "private-content" not in str(call.args)
    assert "Content-Length: 15" in call.args
    assert (
        call.args[1]
        == "https://uploads.github.com/repos/owner/repo/releases/12/assets?name=asset.png"
    )


def test_uploader_rejects_wrong_forge():
    uploader = GitHubVerificationUploader()
    with pytest.raises(ValueError):
        asyncio.run(
            uploader.upload(
                "https://other.example/owner/repo/pull/42",
                HEAD,
                VerificationArtifact("x.py", "test", "C1", "text/x-python", b"x"),
            )
        )


def test_publication_entry_point_uses_existing_gh_auth(tmp_path, monkeypatch, capsys):
    from engine.adapters.source_control.github import verification

    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    publish = AsyncMock(
        return_value={"status": "passed", "comment_url": PR + "#issuecomment-1"}
    )
    monkeypatch.setattr(verification, "publish_verification", publish)
    assert (
        verification.main(
            ["--project", str(tmp_path), "--manifest", str(manifest), "--pr", PR]
        )
        == 0
    )
    args = publish.call_args
    assert args.args == (manifest,)
    assert args.kwargs["pr_url"] == PR
    assert args.kwargs["source_control"]._transport.host == "github.com"
    assert asyncio.run(
        args.kwargs["source_control"]._root_path("local-verification")
    ) == str(tmp_path)
    assert "PR comment:" in capsys.readouterr().out


def test_publication_entry_point_reports_rejected_bundle(tmp_path, monkeypatch, capsys):
    from engine.adapters.source_control.github import verification

    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    monkeypatch.setattr(
        verification,
        "publish_verification",
        AsyncMock(side_effect=ValueError("Wrong PR head")),
    )
    assert verification.main(["--manifest", str(manifest), "--pr", PR]) == 2
    assert "Wrong PR head" in capsys.readouterr().err
