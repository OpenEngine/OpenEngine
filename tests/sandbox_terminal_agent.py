"""Deterministic ACP peer that starts the supplied MCP server inside the guest."""
import json
import os
from pathlib import Path
import subprocess
import sys

servers = []
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "initialize":
        result = {"protocolVersion": 1, "agentCapabilities": {}}
    elif method == "session/new":
        servers = request["params"]["mcpServers"]
        result = {"sessionId": "bridge-session"}
    elif method == "session/prompt":
        config = next(s for s in servers if s["name"] == "workflow")
        assert config["command"] == "python3"
        assert not any(k.startswith(("GH_", "GITHUB_", "GITLAB_", "GLAB_")) for k in os.environ)
        assert not os.environ.get("SSH_AUTH_SOCK")
        for path in ('.git-credentials', '.ssh', '.config/gh', '.config/glab-cli'):
            assert not (Path.home() / path).exists()
        assert 'credential' not in Path('.git/config').read_text()
        manifest = Path.home() / ".oe-mcp" / config["args"][-1]
        assert manifest.stat().st_mode & 0o777 == 0o600
        assert not manifest.is_relative_to(Path.cwd())
        shim = subprocess.Popen([config["command"], *config["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        counter = 0
        def rpc(method, params=None):
            global counter
            counter += 1
            shim.stdin.write(json.dumps({"jsonrpc": "2.0", "id": counter, "method": method, "params": params or {}}) + "\n")
            shim.stdin.flush()
            return json.loads(shim.stdout.readline())["result"]
        def tool(name, arguments):
            return rpc("tools/call", {"name": name, "arguments": arguments})
        shim.stdin.write("invalid JSON\n")
        shim.stdin.flush()
        assert json.loads(shim.stdout.readline())["error"]["code"] == -32700
        rpc("initialize", {"protocolVersion": "2024-11-05"})
        names = {t["name"] for t in rpc("tools/list")["tools"]}
        assert {"git_subcommand", "open_pull_request", "complete_step"} <= names
        Path('.').chmod(0o755)
        Path('README.md').chmod(0o644)
        Path("README.md").write_text("implemented inside VM\n")
        # This metadata must never become trusted host configuration.
        Path('.git').chmod(0o755)
        Path('.git/config').chmod(0o644)
        Path('.git/config').write_text('[alias]\n    hostile = !echo untrusted-guest-metadata\n')
        action = sys.argv[1] if len(sys.argv) > 1 else 'publish'
        if action == 'read-only':
            inspected = tool("git_subcommand", {"arguments": ["log", "-1", "--oneline"]})
            assert not inspected.get('isError'), inspected
        if action == 'leak':
            Path('README.md').write_text(json.loads(manifest.read_text())['token'])
        added = tool("git_subcommand", {"arguments": ["add", "README.md"]})
        if action in {'read-only', 'decline', 'leak', 'conflict'}:
            assert added.get('isError'), added
        else:
            assert not added.get("isError"), added
            assert "hostile" not in Path('.git/config').read_text()
            for args in (["commit", "-m", "feat: implement inside VM"], ["push", "origin", "agent/bridge-test"]):
                answer = tool("git_subcommand", {"arguments": args})
                assert not answer.get("isError"), answer
            answer = tool("open_pull_request", {"branch": "agent/bridge-test", "title": "feat: implement inside VM", "body": "Changes made in the guest."})
            assert not answer.get("isError"), answer
        Path('guest-proof.json').write_text(json.dumps({'env': sorted(os.environ), 'home': str(Path.home())}))
        completed = tool("complete_step", {"outcome": "success", "summary": "Implemented", "outputs": {}})
        assert not completed.get("isError"), completed
        shim.stdin.close()
        shim.wait(timeout=10)
        result = {"stopReason": "end_turn"}
    else:
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
