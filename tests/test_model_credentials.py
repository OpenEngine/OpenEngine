"""Only the selected model credential enters a private guest home."""

import asyncio
import json
import os
from pathlib import Path

import pytest

from engine.adapters.sandbox.smolvm import SmolvmSandbox
from engine.graph_runtime_langgraph.sandbox import SandboxACPProvider, sandbox_provider
from engine.graph_runtime_langgraph.sandbox_workspace import WorkspaceCopies
from engine.ports import SandboxSpec
from engine.runtime import model_credentials as credentials
from engine.runtime.model_credentials import ModelCredentialError, host_model_credential
from langgraph_acp import ACPConnectContext, ClaudeACPProvider, CodexACPProvider

CODEX_ACCESS = "test-codex-access-676-private"
CODEX_REFRESH = "test-codex-refresh-676-private"
CLAUDE_TOKEN = "test-claude-token-676-private"
FORGE_TOKEN = "test-forge-token-676-never-forward"


@pytest.fixture
def login_home(tmp_path, monkeypatch):
    root = tmp_path / "host-home"
    root.mkdir()
    # Isolate credential discovery without changing SmolVM's installation home.
    monkeypatch.setattr(credentials, "_host_home", lambda: root)
    for key in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(credentials, "_claude_keychain", lambda: None)
    (root / ".codex").mkdir()
    (root / ".codex/auth.json").write_text(json.dumps({
        "auth_mode": "chatgpt", "tokens": {"access_token": CODEX_ACCESS, "refresh_token": CODEX_REFRESH,
        "id_token": "test-id-token-676", "account_id": "test-account-676", "GITHUB_TOKEN": FORGE_TOKEN},
        "github": FORGE_TOKEN,
    }))
    (root / ".claude").mkdir()
    (root / ".claude/.credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": CLAUDE_TOKEN}, "gitlab": FORGE_TOKEN}))
    (root / ".git-credentials").write_text(FORGE_TOKEN)
    (root / ".gitconfig").write_text('[credential]\nhelper = ' + FORGE_TOKEN)
    (root / ".config/gh").mkdir(parents=True)
    (root / ".config/gh/hosts.yml").write_text(FORGE_TOKEN)
    (root / ".config/glab-cli").mkdir()
    (root / ".config/glab-cli/config.yml").write_text(FORGE_TOKEN)
    (root / ".ssh").mkdir()
    (root / ".ssh/id_ed25519").write_text(FORGE_TOKEN)
    for key in ("GITHUB_TOKEN", "GH_TOKEN", "GITLAB_TOKEN", "GL_TOKEN", "GIT_ASKPASS", "SSH_AUTH_SOCK"):
        monkeypatch.setenv(key, FORGE_TOKEN)
    return root


@pytest.fixture(params=["smolvm-fake", pytest.param("smolvm-real", marks=pytest.mark.integration)])
def vm_backend(request):
    if request.param == "smolvm-fake":
        launcher, root = request.getfixturevalue("smolvm_cli")
        return SmolvmSandbox(image="fixture", executable=str(launcher)), root
    image = os.environ.get("OE_SMOLVM_TEST_IMAGE")
    if not image:
        pytest.skip("set OE_SMOLVM_TEST_IMAGE for real tmpfs credential isolation checks")
    return SmolvmSandbox(image=image, executable=os.environ.get("OE_SMOLVM_EXECUTABLE", "smolvm")), None


async def command(vm, script):
    process = await vm.exec(("python3", "-c", script))
    process.stdin.close()
    stdout, stderr, code = await asyncio.gather(process.stdout.read(), process.stderr.read(), process.wait())
    assert code == 0, stderr.decode()
    return json.loads(stdout)


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_discovery_reads_only_selected_provider_and_filters_extra_fields(login_home, provider, monkeypatch):
    reads = []
    original = credentials._read
    def read(path):
        reads.append(path)
        return original(path)
    monkeypatch.setattr(credentials, "_read", read)
    selected = host_model_credential(provider, {})
    assert selected.provider == provider
    assert FORGE_TOKEN not in selected.value
    assert (CODEX_ACCESS in selected.value) == (provider == "codex")
    assert (CLAUDE_TOKEN in selected.value) == (provider == "claude")
    assert len(reads) == 1
    assert reads[0] == login_home / (".codex/auth.json" if provider == "codex" else ".claude/.credentials.json")
    assert selected.value not in repr(selected)
    assert selected.value not in repr(selected.home())
    assert selected.value not in repr(SandboxSpec(home=selected.home()))


def test_custom_account_directory_never_falls_back_to_other_claude_account(login_home, monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "missing-account"))
    monkeypatch.setattr(credentials, "_claude_keychain", lambda: pytest.fail("must not read a different account"))
    with pytest.raises(ModelCredentialError, match="unavailable"):
        host_model_credential("claude", {})


def test_codex_account_directory_is_resolved_per_invocation(login_home, tmp_path):
    other = tmp_path / "selected-codex"
    other.mkdir()
    (other / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": "selected-explicit-model-key"}))
    assert host_model_credential("codex", {"CODEX_HOME": str(other)}).value == '{"OPENAI_API_KEY": "selected-explicit-model-key"}'
    assert CODEX_ACCESS in host_model_credential("codex", {}).value


def test_claude_selects_fresh_keychain_token_and_excludes_metadata(login_home, monkeypatch):
    monkeypatch.setattr(credentials, "_claude_keychain", lambda: json.dumps({
        "claudeAiOauth": {"accessToken": "fresh-claude-keychain-token", "expiresAt": 9999999999999},
        "github": FORGE_TOKEN,
    }))
    assert host_model_credential("claude", {}).value == "fresh-claude-keychain-token"


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_only_selected_credential_is_in_tmpfs_and_never_in_workspace_or_patch(vm_backend, login_home, tmp_path, provider):
    backend, fake_root = vm_backend
    selected = host_model_credential(provider, {})
    source = tmp_path / "repo"
    source.mkdir()
    (source / "code.py").write_text("old content\n")
    observed = []

    async def run():
        async with WorkspaceCopies().create(str(source), "apply") as copy:
            async with copy.backend(backend).create(SandboxSpec(home=selected.home())) as vm:
                result = await command(vm, '''from pathlib import Path
import json, os, stat, subprocess
home = Path(os.environ['HOME'])
root = Path.cwd()
assert not home.is_relative_to(root)
assert stat.S_IMODE(home.stat().st_mode) == 0o700
for file in home.rglob('*'):
    if file.is_file():
        assert stat.S_IMODE(file.stat().st_mode) == 0o600
assert not (home / '.git-credentials').exists()
assert not (home / '.gitconfig').exists()
assert not (home / '.ssh').exists()
assert not (home / '.config/gh').exists()
assert not (home / '.config/glab-cli').exists()
assert not any(k.startswith(('GITHUB_', 'GH_', 'GITLAB_')) or k in ('GL_TOKEN', 'GIT_ASKPASS', 'SSH_AUTH_SOCK') for k in os.environ)
assert subprocess.run(['git', 'config', '--get-all', 'credential.helper'], capture_output=True).returncode == 1
codex = home / '.codex/auth.json'
claude = home / '.claude/model-token'
if codex.exists():
    data = json.loads(codex.read_text())
    assert set(data['tokens']) <= {'access_token', 'refresh_token', 'id_token', 'account_id'}
    assert not claude.exists()
    assert 'CLAUDE_CODE_OAUTH_TOKEN' not in os.environ
else:
    assert claude.exists() and not codex.exists()
    assert os.environ['CLAUDE_CODE_OAUTH_TOKEN'] == claude.read_text()
for file in root.rglob('*'):
    assert not file.name in ('auth.json', 'model-token', '.credentials.json', '.git-credentials')
Path('code.py').write_text('node output\\n')
print(json.dumps({'home': str(home), 'files': [str(p.relative_to(home)) for p in home.rglob('*') if p.is_file()]}))
''')
                observed.append(result["home"])
                if fake_root is None:
                    await command(vm, f"from pathlib import Path; assert not Path({str(login_home)!r}).exists(); print('{{}}')")
            patch = await copy.accept()
            assert b"node output" in patch
            for secret in (CODEX_ACCESS, CODEX_REFRESH, CLAUDE_TOKEN, FORGE_TOKEN):
                assert secret.encode() not in patch
    asyncio.run(run())
    assert (source / "code.py").read_text() == "node output\n"
    if fake_root is not None:
        assert observed and not Path(observed[0]).exists()
        assert not list(fake_root.glob("oe-sandbox-*"))
        commands = (fake_root / "commands.jsonl").read_text()
        assert not any(secret in commands for secret in (CODEX_ACCESS, CODEX_REFRESH, CLAUDE_TOKEN, FORGE_TOKEN))
        assert "--volume" not in commands and "--ssh-agent" not in commands


@pytest.mark.parametrize("key", ["GITHUB_TOKEN", "GH_TOKEN", "GITLAB_TOKEN", "GL_TOKEN", "GIT_ASKPASS", "SSH_AUTH_SOCK"])
def test_forge_credentials_are_refused_before_creating_a_vm(key, tmp_path):
    class NeverStart:
        def create(self, spec):
            pytest.fail("must refuse forge credentials before launching")
    provider = SandboxACPProvider(name="test", command=("python3",), backend=NeverStart(), env={key: FORGE_TOKEN})
    async def run():
        with pytest.raises(ModelCredentialError, match="forge credentials"):
            await provider.connect(context=ACPConnectContext(workspace=str(tmp_path)))
    asyncio.run(run())


@pytest.mark.parametrize("provider_type,kind", [(CodexACPProvider, "codex"), (ClaudeACPProvider, "claude")])
def test_renamed_providers_keep_their_credential_identity(provider_type, kind):
    adapted = sandbox_provider(provider_type(name="custom-agent"), object())
    assert adapted.name == "custom-agent"
    assert adapted.model_provider == kind


@pytest.mark.parametrize("outcome", ["complete", "failure", "cancel"])
def test_private_home_is_removed_on_every_teardown(vm_backend, login_home, outcome):
    backend, fake_root = vm_backend
    names = []
    homes = []
    async def run():
        started = asyncio.Event()
        instances = []
        async def body():
            async with backend.create(SandboxSpec(home=host_model_credential("codex", {}).home())) as vm:
                instances.append(vm)
                names.append(vm.name)
                homes.append(await command(vm, "import json, os; print(json.dumps(os.environ['HOME']))"))
                started.set()
                if outcome == "failure":
                    message = "intentional test failure"
                    raise ValueError(message)
                if outcome == "cancel":
                    await asyncio.Event().wait()
        task = asyncio.create_task(body())
        await started.wait()
        if outcome == "cancel":
            task.cancel()
        if outcome in {"failure", "cancel"}:
            with pytest.raises(ValueError if outcome == "failure" else asyncio.CancelledError):
                await task
        else:
            await task
        stdout, _, _ = await instances[0]._command(["machine", "ls", "--json"])
        assert names[0] not in [entry["name"] for entry in json.loads(stdout)]
    asyncio.run(run())
    if fake_root is not None:
        assert not Path(homes[0]).exists()
        assert not list(fake_root.glob("oe-sandbox-*"))


def test_credentials_cannot_become_a_copied_out_patch(vm_backend, login_home, tmp_path):
    from engine.graph_runtime_langgraph.sandbox_workspace import WorkspaceCredentialLeak
    backend, _ = vm_backend
    source = tmp_path / "repo"
    source.mkdir()
    (source / "code.py").write_text("unchanged")
    async def run():
        async with WorkspaceCopies().create(str(source), "apply") as copy:
            async with copy.backend(backend).create(SandboxSpec(home=host_model_credential("codex", {}).home())) as vm:
                await command(vm, """from pathlib import Path
import json, os
credential = json.loads((Path(os.environ['CODEX_HOME']) / 'auth.json').read_text())['tokens']['access_token']
Path('accidental-output.bin').write_bytes(b'\\x00' + credential.encode())
print('{}')
""")
            with pytest.raises(WorkspaceCredentialLeak, match="patch was not created") as caught:
                await copy.accept()
            assert CODEX_ACCESS not in str(caught.value)
            assert not hasattr(caught.value, "patch")
    asyncio.run(run())
    assert not (source / "accidental-output.bin").exists()
    assert (source / "code.py").read_text() == "unchanged"


@pytest.mark.parametrize("provider_type,kind", [(CodexACPProvider, "codex"), (ClaudeACPProvider, "claude")])
def test_graph_runtime_injects_selected_login_before_launch(vm_backend, login_home, tmp_path, provider_type, kind):
    from engine.graph_runtime_langgraph import ACPNode
    from langgraph_acp import ACPAgentRegistry
    from test_sandbox_acp_provider import run_node
    backend, _ = vm_backend
    source = tmp_path / "repo"
    source.mkdir()
    (source / "agent.py").write_text('''import json, os, pathlib, sys
for line in sys.stdin:
    r = json.loads(line)
    method = r.get('method')
    if method == 'initialize':
        result = {'protocolVersion': 1, 'agentCapabilities': {}}
    elif method == 'session/new':
        result = {'sessionId': 'credential-session'}
    elif method == 'session/prompt':
        home = pathlib.Path(os.environ['HOME'])
        assert not home.is_relative_to(pathlib.Path.cwd())
        kind = 'codex' if (home / '.codex/auth.json').exists() else 'claude'
        if kind == 'claude':
            assert os.environ['CLAUDE_CODE_OAUTH_TOKEN'] == (home / '.claude/model-token').read_text()
        pathlib.Path('result.txt').write_text(kind)
        text = json.dumps({'provider': kind, 'home': str(home)})
        print(json.dumps({'jsonrpc': '2.0', 'method': 'session/update', 'params': {'sessionId': 'credential-session', 'update': {'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': text}}}}), flush=True)
        result = {'stopReason': 'end_turn'}
    else:
        continue
    print(json.dumps({'jsonrpc': '2.0', 'id': r['id'], 'result': result}), flush=True)
''')
    provider = provider_type(name="renamed-agent", command=("python3", "agent.py"))
    node = ACPNode(agent=provider.name, registry=ACPAgentRegistry([provider]), cwd=str(source), prompt="probe")
    result = json.loads(asyncio.run(run_node(node, backend=backend, timeout=180, model_credentials=host_model_credential)))
    assert result["provider"] == kind
    assert (source / "result.txt").read_text() == kind
    if isinstance(backend, SmolvmSandbox) and vm_backend[1] is not None:
        assert not Path(result["home"]).exists()


@pytest.mark.parametrize("value", ["not json", '{"GITHUB_TOKEN":"private-forge"}', '{"tokens": {"access_token": null}}'])
def test_invalid_codex_login_never_echoes_credential_material(value):
    from engine.runtime.model_credentials import ModelCredential
    with pytest.raises(ModelCredentialError) as caught:
        ModelCredential("codex", value)
    assert value not in str(caught.value)


@pytest.mark.parametrize("provider,other", [("codex", "ANTHROPIC_API_KEY"), ("claude", "OPENAI_API_KEY")])
def test_a_node_cannot_receive_another_model_credential(provider, other, tmp_path):
    node = SandboxACPProvider(name="test", model_provider=provider, command=("python3",), backend=object(), env={other: "other-provider-secret"})
    async def run():
        with pytest.raises(ModelCredentialError, match="only its selected"):
            await node.connect(context=ACPConnectContext(workspace=str(tmp_path)))
    asyncio.run(run())


def test_missing_tmpfs_fails_without_writing_a_credential(tmp_path, monkeypatch):
    from engine.adapters.sandbox.smolvm import _guest
    monkeypatch.setattr(_guest, "MEMORY_HOME_ROOT", tmp_path)
    monkeypatch.setattr(_guest, "_is_tmpfs", lambda _: False)
    with pytest.raises(RuntimeError, match="require a tmpfs"):
        _guest.create_home({"files": {".codex/auth.json": CODEX_ACCESS}, "paths": {}, "env_files": {}})
    assert not list(tmp_path.iterdir())


def test_explicit_selected_credential_wins_over_ambient_other_login(login_home, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "ambient-claude-token")
    selected = host_model_credential("claude", {"ANTHROPIC_API_KEY": "explicit-claude-api-key"})
    assert selected.kind == "api-key" and selected.value == "explicit-claude-api-key"


def test_private_home_cannot_be_exported_as_a_workspace(vm_backend, login_home, tmp_path):
    backend, _ = vm_backend
    async def run():
        async with backend.create(SandboxSpec(home=host_model_credential("codex", {}).home())) as vm:
            home = await command(vm, "import json, os; print(json.dumps(os.environ['HOME']))")
            with pytest.raises(ValueError, match="workspace"):
                await vm.copy_out(home, tmp_path / "exported-home")
    asyncio.run(run())
    assert not (tmp_path / "exported-home").exists()


def test_wrong_provider_from_credential_source_is_refused(tmp_path):
    from engine.runtime.model_credentials import ModelCredential
    provider = SandboxACPProvider(
        name="codex", command=("python3",), backend=object(), model_provider="codex",
        credential_source=lambda _provider, _values: ModelCredential("claude", CLAUDE_TOKEN),
    )
    async def run():
        with pytest.raises(ModelCredentialError, match="different model provider"):
            await provider.connect(context=ACPConnectContext(workspace=str(tmp_path)))
    asyncio.run(run())


def test_transfer_errors_redact_selected_login_tokens(login_home, monkeypatch):
    from engine.adapters.sandbox.smolvm import SmolvmInstance
    instance = SmolvmInstance("unused", "test", SandboxSpec(home=host_model_credential("codex", {}).home()))
    async def failure(*args, **kwargs):
        return json.dumps({"error": "ValueError", "message": CODEX_ACCESS + " " + CODEX_REFRESH}).encode(), b"", 2
    monkeypatch.setattr(instance, "_command", failure)
    async def run():
        with pytest.raises(ValueError) as caught:
            await instance._helper("init")
        assert CODEX_ACCESS not in str(caught.value)
        assert CODEX_REFRESH not in str(caught.value)
        assert "[redacted]" in str(caught.value)
    asyncio.run(run())


@pytest.mark.parametrize("provider,variable", [("codex", "OPENAI_API_KEY"), ("claude", "ANTHROPIC_API_KEY")])
def test_explicit_api_key_is_stored_in_only_the_selected_private_home(provider, variable):
    from engine.runtime.model_credentials import explicit_model_credential
    selected = explicit_model_credential(provider, {variable: "selected-model-api-key-676"})
    assert selected is not None and selected.kind == "api-key"
    home = selected.home()
    if provider == "codex":
        assert json.loads(home.files[".codex/auth.json"]) == {"OPENAI_API_KEY": "selected-model-api-key-676"}
        assert not home.env_files
        assert not any(path.startswith(".claude/") for path in home.files)
    else:
        assert home.files[".claude/model-token"] == "selected-model-api-key-676"
        assert home.env_files == {"ANTHROPIC_API_KEY": ".claude/model-token"}
        assert not any(path.startswith(".codex/") for path in home.files)
