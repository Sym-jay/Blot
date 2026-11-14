import io
import json
import os
import subprocess
import sys

import pytest

from blot import hooks
from blot.mcp import LEGACY_VERSIONS, MODERN_VERSIONS, serve

from test_checks import GUARD
from test_why_github import SPLIT, history

MODERN = {"_meta": {"io.modelcontextprotocol/protocolVersion": MODERN_VERSIONS[0],
                    "io.modelcontextprotocol/clientCapabilities": {}}}


def session(repo, messages):
    """Run the server over in-memory streams; return {id: response} and check stdout is pure JSON-RPC."""
    stdin = io.StringIO("".join(m if isinstance(m, str) else json.dumps(m) + "\n" for m in messages))
    stdout = io.StringIO()
    assert serve(stdin, stdout, str(repo)) == 0
    lines = stdout.getvalue().splitlines()
    replies = [json.loads(line) for line in lines]  # every line is one JSON message
    assert all(r["jsonrpc"] == "2.0" for r in replies)
    return {r.get("id"): r for r in replies}


def call(msg_id, name, arguments, modern=False):
    params = {"name": name, "arguments": arguments, **(MODERN if modern else {})}
    return {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call", "params": params}


def test_legacy_session(make_repo, capsys):
    repo = make_repo(history())
    replies = session(repo, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        call(3, "explain_lines", {"path": "core.py", "start": 3}),
        call(4, "load_bearing_lines", {"path": "core.py"}),
        call(5, "load_bearing_lines", {"path": "missing.py"}),
        call(6, "explain_lines", {"path": "core.py", "start": 999}),
        call(7, "explain_lines", {"path": "core.py", "start": "3"}),
        call(8, "explain_lines", {"path": "core.py"}),
        call(9, "no_such_tool", {}),
        "this is not json\n",
        {"jsonrpc": "2.0", "id": 10, "method": "resources/list"},
    ])
    assert len(replies) == 11  # the notification got no reply
    init = replies[1]["result"]
    assert init["protocolVersion"] == "2025-06-18"
    assert init["capabilities"] == {"tools": {}} and init["serverInfo"]["name"] == "blot"

    tools = replies[2]["result"]["tools"]
    assert [t["name"] for t in tools] == ["explain_lines", "load_bearing_lines"]
    assert all(t["inputSchema"]["type"] == "object" and "outputSchema" in t for t in tools)

    why = replies[3]["result"]
    assert why["isError"] is False
    (text,) = why["content"]
    assert text["type"] == "text"
    assert "core.py:3, explained by commit:" in text["text"]
    assert '"Break reference cycle in App (fixes #3761)" [bug fix], references #3761: line 3' in text["text"]
    assert f'Traced past refactor {why["structuredContent"]["ranges"][0]["via"][0][:7]} "{SPLIT}"' in text["text"]
    assert why["structuredContent"]["ranges"][0]["text"] == [f"        {GUARD}"]

    fixmap = replies[4]["result"]
    assert fixmap["isError"] is False
    body = fixmap["content"][0]["text"]
    assert body.startswith("core.py (4 lines): 2 lines exist because of 1 bug fix.")
    assert f"lines 3-4 (`{GUARD}`)" in body and SPLIT in body
    ((r,),) = [fixmap["structuredContent"]["ranges"]]
    assert (r["start"], r["end"]) == (3, 4)

    for msg_id, needle in [(5, "not tracked"), (6, "only 4 lines"), (7, "must be an integer"), (8, "missing argument 'start'")]:
        result = replies[msg_id]["result"]
        assert result["isError"] is True, msg_id
        assert needle in result["content"][0]["text"], msg_id

    assert replies[9]["error"]["code"] == -32602 and "Unknown tool" in replies[9]["error"]["message"]
    assert replies[None]["error"]["code"] == -32700
    assert replies[10]["error"]["code"] == -32601
    assert capsys.readouterr().out == ""  # nothing leaked to the real stdout either


def test_modern_session(make_repo):
    repo = make_repo(history())
    replies = session(repo, [
        {"jsonrpc": "2.0", "id": "d", "method": "server/discover", "params": MODERN},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": MODERN},
        call(2, "load_bearing_lines", {"path": "core.py", "rev": "HEAD"}, modern=True),
        call(3, "explain_lines", {"path": "../outside.py", "start": 1}, modern=True),
        {"jsonrpc": "2.0", "id": 4, "method": "tools/list"},  # no _meta and no initialize
        {"jsonrpc": "2.0", "id": 5, "method": "tools/list", "params": {"_meta": {
            "io.modelcontextprotocol/protocolVersion": "1900-01-01", "io.modelcontextprotocol/clientCapabilities": {}}}},
    ])
    discover = replies["d"]["result"]
    assert discover["resultType"] == "complete"
    assert discover["supportedVersions"] == list(MODERN_VERSIONS)
    assert discover["capabilities"] == {"tools": {}}
    assert discover["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "blot"
    assert len(replies[1]["result"]["tools"]) == 2
    assert replies[2]["result"]["isError"] is False and replies[2]["result"]["resultType"] == "complete"
    assert replies[3]["result"]["isError"] is True
    assert replies[4]["error"]["code"] == -32602
    assert replies[5]["error"]["code"] == -32022
    assert replies[5]["error"]["data"] == {"supported": list(MODERN_VERSIONS), "requested": "1900-01-01"}


def test_unknown_legacy_version_gets_our_latest(make_repo):
    repo = make_repo(history())
    replies = session(repo, [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2099-01-01"}}])
    assert replies[1]["result"]["protocolVersion"] == LEGACY_VERSIONS[0]


def test_not_a_git_repo(tmp_path):
    replies = session(tmp_path, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
        call(2, "load_bearing_lines", {"path": "a.py"}),
    ])
    assert replies[2]["result"]["isError"] is True


def test_blot_mcp_subprocess(make_repo):
    """The real command: UTF-8 JSON lines on stdout, logs on stderr, exits when stdin closes."""
    repo = make_repo(history())
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
        call(2, "load_bearing_lines", {"path": "core.py"}),
    ]
    proc = subprocess.run(
        [sys.executable, "-m", "blot", "mcp"], cwd=repo, capture_output=True, timeout=60,
        input="".join(json.dumps(m) + "\n" for m in messages).encode(),
    )
    assert proc.returncode == 0
    replies = [json.loads(line) for line in proc.stdout.decode("utf-8").splitlines()]
    assert [r["id"] for r in replies] == [1, 2]
    assert replies[1]["result"]["isError"] is False
    assert b"first run, indexing" in proc.stderr


# --- install-mcp ------------------------------------------------------------------------


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def test_install_mcp_writes_mcp_json_and_uninstalls(make_repo):
    repo = str(make_repo(history()))
    path = hooks.mcp_config_path(repo)
    assert hooks.install_mcp(repo, python="C:\\py\\python.exe") == ("installed", path)
    assert read_json(path) == {"mcpServers": {"blot": {
        "type": "stdio", "command": "C:/py/python.exe", "args": ["-m", "blot", "mcp"]}}}
    assert hooks.install_mcp(repo, python="C:\\py\\python.exe")[0] == "already installed"
    assert hooks.install_mcp(repo, python="/venv/bin/python")[0] == "updated"
    assert read_json(path)["mcpServers"]["blot"]["command"] == "/venv/bin/python"
    assert hooks.uninstall_mcp(repo) == "removed"
    assert not os.path.exists(path)
    assert hooks.uninstall_mcp(repo) == "not installed"


def test_install_mcp_keeps_other_servers(make_repo):
    repo = str(make_repo(history()))
    path = hooks.mcp_config_path(repo)
    other = {"mcpServers": {"docs": {"command": "node", "args": ["docs.js"]}}, "x": 1}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(other, f)
    assert hooks.install_mcp(repo, python="/py")[0] == "installed"
    assert set(read_json(path)["mcpServers"]) == {"docs", "blot"}
    assert hooks.uninstall_mcp(repo) == "removed"
    assert read_json(path) == other


def test_install_mcp_refuses_bad_json_and_name_clash(make_repo):
    repo = str(make_repo(history()))
    path = hooks.mcp_config_path(repo)
    with open(path, "w", encoding="utf-8") as f:
        f.write("{not json")
    with pytest.raises(ValueError):
        hooks.install_mcp(repo, python="/py")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"mcpServers": {"blot": {"command": "something-else"}}}, f)
    with pytest.raises(ValueError, match="isn't `blot mcp`"):
        hooks.install_mcp(repo, python="/py")
    assert hooks.uninstall_mcp(repo) == "not installed"


def test_install_mcp_local_needs_claude_cli(make_repo, monkeypatch):
    import shutil

    repo = str(make_repo(history()))
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="claude mcp add --scope local --transport stdio blot --"):
        hooks.install_mcp(repo, python="/py", local=True)
