"""`blot mcp`: a stdio MCP server, so coding agents can read a file's history before they plan an edit.

Tools:
    explain_lines(path, start, end?, rev?)      what `blot why` says about those lines
    load_bearing_lines(path, rev?, min_severity?)   what `blot map` says about the file

Standard library only, like the rest of Blot. Speaks both eras of the protocol:
    - 2026-07-28 (stateless): every request carries `_meta["io.modelcontextprotocol/protocolVersion"]`;
      `server/discover` lists the versions we support
    - 2025-11-25 and earlier: an `initialize` handshake, then `notifications/initialized`
Transport: one JSON-RPC message per line on stdin/stdout. Only protocol messages go to stdout; Blot's
own messages (index progress) and logs go to stderr. A bad path, an untracked file or a repo
without git history comes back as a tool result with `isError: true`; nothing here ends the server
but the end of stdin.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import traceback
from typing import Any, TextIO

from blot import __version__

MODERN_VERSIONS = ("2026-07-28",)
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
_META = "io.modelcontextprotocol/"
SERVER_INFO = {"name": "blot", "title": "Blot", "version": __version__}

INSTRUCTIONS = (
    "Blot reads this repository's git history. Before changing code you didn't write, call "
    "load_bearing_lines on the file to see which lines exist because of bug fixes, and explain_lines "
    "on the lines you plan to touch. A line marked as a fix may be guarding against a bug coming back: "
    "read the fix commit and its issue before removing or rewriting it."
)

_PATH = {"type": "string", "description": "File path, relative to the repository root (or absolute)"}
_REV = {"type": "string", "description": "Read the file as of this commit (default: HEAD)"}
_RANGE = {
    "type": "object",
    "properties": {
        "start": {"type": "integer"}, "end": {"type": "integer"},
        "commit": {"type": "string"}, "via": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["start", "end", "commit", "via"],
}

TOOLS = [
    {
        "name": "explain_lines",
        "title": "Explain lines from git history",
        "description": (
            "Explain why lines of a file exist: the commit that introduced each line, seeing past commits "
            "that only moved code (renames, file splits, reformatting). Says whether that commit was a bug "
            "fix and which issues it references. Same as `blot why FILE:START-END`."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": _PATH,
                "start": {"type": "integer", "minimum": 1, "description": "First line (1-based)"},
                "end": {"type": "integer", "minimum": 1, "description": "Last line (default: start)"},
                "rev": _REV,
            },
            "required": ["path", "start"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"}, "rev": {"type": "string"},
                "ranges": {"type": "array", "items": _RANGE},
                "commits": {"type": "object"},
            },
            "required": ["path", "rev", "ranges", "commits"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "load_bearing_lines",
        "title": "Find lines added by bug fixes",
        "description": (
            "List the lines of a file that exist because of a bug fix, grouped by fix: each fix's commit, "
            "date, subject and linked issues, and the line ranges it explains (traced past refactors). "
            "Changing these lines may bring the bug back. Same as `blot map FILE`."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": _PATH,
                "rev": _REV,
                "min_severity": {
                    "type": "integer", "enum": [1, 2],
                    "description": "2 (default): bug fixes only. 1: also routine fixes (typos, small corrections)",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"}, "rev": {"type": "string"},
                "line_count": {"type": "integer"}, "min_severity": {"type": "integer"},
                "ranges": {"type": "array", "items": _RANGE},
                "commits": {"type": "object"},
            },
            "required": ["path", "rev", "line_count", "min_severity", "ranges", "commits"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
]


class ToolError(Exception):
    """A problem the agent can fix (bad path, bad line numbers): reported with isError: true."""


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


# --- the tools --------------------------------------------------------------------------------


def _commit_text(info: dict, kind: bool = False) -> str:
    """'8efea0c (2020-10-03) "subject" [bug fix], references #3761 "title"'."""
    issues = ", ".join(f'{i["ref"]} "{i["title"]}"' if i["title"] else i["ref"] for i in info["issues"])
    text = f'{info["short"]} ({info["date"]}) "{info["subject"]}"' + (f' [{info["kind"]}]' if kind else "")
    return text + (f", references {issues}" if issues else "")


def _via_text(via: list[str], commits: dict) -> str:
    if not via:
        return ""
    hops = "; ".join(f'{commits[v]["short"]} "{commits[v]["subject"]}"' for v in via)
    return f" Traced past refactor{'s' * (len(via) > 1)} {hops}, which only moved the code."


def _excerpt(text: str, limit: int = 70) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _where(r: dict) -> str:
    return f'line {r["start"]}' if r["start"] == r["end"] else f'lines {r["start"]}-{r["end"]}'


def _paragraphs(data: dict, kinds: bool) -> list[str]:
    """One paragraph per commit, listing the ranges it explains, in order of first appearance."""
    by_commit: dict[str, list[dict]] = {}
    for r in data["ranges"]:
        by_commit.setdefault(r["commit"], []).append(r)
    out = []
    for sha, ranges in by_commit.items():
        info = data["commits"][sha]
        where = ", ".join(f'{_where(r)} (`{_excerpt(r["text"][0])}`)' for r in ranges)
        via = list(dict.fromkeys(v for r in ranges for v in r["via"]))
        out.append(f"{_commit_text(info, kinds)}: {where}.{_via_text(via, data['commits'])}")
    return out


def load_bearing_lines(store, args: dict) -> tuple[str, dict]:
    from blot.config import load_config
    from blot.fixmap import build_map, to_json

    min_severity = args.get("min_severity") or load_config(store.repo).min_severity
    fmap = build_map(store, args["path"], args.get("rev") or "HEAD", min_severity)
    data = to_json(store, fmap)
    at = f' at {data["rev"]}' if data["rev"] != "HEAD" else ""
    if not data["ranges"]:
        what = "bug fix" if min_severity >= 2 else "fix"
        return (f'{data["path"]}{at} ({data["line_count"]} lines): no line comes from a {what}. '
                "Nothing here is known to guard against a past bug."), data
    lines = sum(len(r["lines"]) for r in data["ranges"])
    fixes = len({r["commit"] for r in data["ranges"]})
    head = (f'{data["path"]}{at} ({data["line_count"]} lines): {lines} line{"s" * (lines != 1)} exist because of '
            f'{fixes} {"bug " if min_severity >= 2 else ""}fix{"es" * (fixes != 1)}. Changing them may bring '
            "the bug back; read the fix (git show SHA) before you do.")
    return "\n\n".join([head, *_paragraphs(data, kinds=False)]), data


def explain_lines(store, args: dict) -> tuple[str, dict]:
    from blot.why import commit_info, explain

    start = args["start"]
    end = args.get("end") or start
    rev = args.get("rev") or "HEAD"
    path, explained = explain(store, f'{args["path"]}:{start}-{end}', rev)
    ranges: list[dict] = []
    for e in explained:  # consecutive lines with the same story become one range
        last = ranges[-1] if ranges else None
        if last and last["commit"] == e.sha and last["via"] == e.via and last["end"] == e.line - 1:
            last["end"] = e.line
            last["text"].append(e.text)
        else:
            ranges.append({"start": e.line, "end": e.line, "text": [e.text], "commit": e.sha, "via": e.via})
    cited = list(dict.fromkeys([*(r["commit"] for r in ranges), *(v for r in ranges for v in r["via"])]))
    data = {"path": path, "rev": rev, "ranges": ranges, "commits": {sha: commit_info(store, sha) for sha in cited}}
    if not ranges:
        return f"{path}:{start}-{end}: no lines to explain (blank, or not in history).", data
    head = f"{path}:{start}" + (f"-{end}" if end != start else "") + (f" at {rev}" if rev != "HEAD" else "")
    return "\n\n".join([f"{head}, explained by commit:", *_paragraphs(data, kinds=True)]), data


HANDLERS = {"explain_lines": explain_lines, "load_bearing_lines": load_bearing_lines}


def _check_args(name: str, args: dict) -> None:
    """Enough validation for a helpful message; the schemas in TOOLS say the same thing."""
    schema = next(t["inputSchema"] for t in TOOLS if t["name"] == name)
    for key in schema["required"]:
        if key not in args:
            raise ToolError(f"missing argument {key!r}")
    for key, value in args.items():
        spec = schema["properties"].get(key)
        if spec is None:
            raise ToolError(f"unknown argument {key!r}")
        if spec["type"] == "integer":
            if not isinstance(value, int) or isinstance(value, bool):
                raise ToolError(f"{key} must be an integer, got {value!r}")
            if value < spec.get("minimum", value) or ("enum" in spec and value not in spec["enum"]):
                raise ToolError(f"{key} out of range: {value!r}")
        elif spec["type"] == "string" and (not isinstance(value, str) or not value):
            raise ToolError(f"{key} must be a non-empty string")
    if args.get("end") is not None and args["end"] < args["start"]:
        raise ToolError(f"end ({args['end']}) is before start ({args['start']})")


# --- the protocol -----------------------------------------------------------------------------


class Server:
    def __init__(self, repo: str = "."):
        self.repo = repo
        self.legacy_version: str | None = None  # set by `initialize` (2025-11-25 and earlier)

    def _version(self, params: dict, method: str) -> str:
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        version = meta.get(_META + "protocolVersion")
        if version is None:
            if self.legacy_version is not None or method == "ping":
                return self.legacy_version or LEGACY_VERSIONS[0]
            raise RpcError(-32602, "missing _meta io.modelcontextprotocol/protocolVersion (or send initialize first)")
        if version not in MODERN_VERSIONS:
            raise RpcError(-32022, "Unsupported protocol version",
                           {"supported": list(MODERN_VERSIONS), "requested": version})
        if not isinstance(meta.get(_META + "clientCapabilities"), dict):
            raise RpcError(-32602, "missing _meta io.modelcontextprotocol/clientCapabilities")
        return version

    def _result(self, version: str, body: dict) -> dict:
        body = {"resultType": "complete", **body}
        if version in MODERN_VERSIONS:
            body["_meta"] = {**body.get("_meta", {}), _META + "serverInfo": SERVER_INFO}
        return body

    def handle(self, message: Any) -> dict | None:
        """One JSON-RPC message in, the response out (None for notifications)."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            if isinstance(message, dict) and "method" not in message and ("result" in message or "error" in message):
                return None  # a response: we never send requests, so there is nothing to match it to
            return _error(message.get("id") if isinstance(message, dict) else None, -32600, "Invalid Request")
        method, msg_id = message["method"], message.get("id")
        params = message.get("params") or {}
        if "id" not in message:  # notification: initialized, cancelled, ...
            return None
        if not isinstance(params, dict):
            return _error(msg_id, -32602, "params must be an object")
        try:
            return {"jsonrpc": "2.0", "id": msg_id, "result": self._dispatch(method, params)}
        except RpcError as exc:
            return _error(msg_id, exc.code, exc.message, exc.data)
        except Exception as exc:  # a bug in Blot: report it, keep serving
            traceback.print_exc(file=sys.stderr)
            return _error(msg_id, -32603, f"internal error: {exc.__class__.__name__}: {exc}")

    def _dispatch(self, method: str, params: dict) -> dict:
        if method == "initialize":
            requested = params.get("protocolVersion")
            self.legacy_version = requested if requested in LEGACY_VERSIONS else LEGACY_VERSIONS[0]
            return {
                "protocolVersion": self.legacy_version,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
                "instructions": INSTRUCTIONS,
            }
        version = self._version(params, method)
        if method == "server/discover":
            return self._result(version, {
                "supportedVersions": list(MODERN_VERSIONS),
                "capabilities": {"tools": {}},
                "instructions": INSTRUCTIONS,
            })
        if method == "ping":
            return self._result(version, {})
        if method == "tools/list":
            return self._result(version, {"tools": TOOLS})
        if method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            if name not in HANDLERS:
                raise RpcError(-32602, f"Unknown tool: {name}")
            if not isinstance(args, dict):
                raise RpcError(-32602, "arguments must be an object")
            return self._result(version, self.call(name, args))
        raise RpcError(-32601, f"Method not found: {method}")

    def call(self, name: str, args: dict) -> dict:
        """Run a tool. Every failure becomes an isError result the agent can read and act on."""
        from blot.index import open_index
        from blot.why import WhyError

        store = None
        try:
            _check_args(name, args)
            # stray prints from anything below must not corrupt the protocol stream
            with contextlib.redirect_stdout(sys.stderr):
                store = open_index(self.repo)
                if store is None:
                    raise ToolError("could not open Blot's history index")
                text, data = HANDLERS[name](store, args)
        except (ToolError, WhyError) as exc:
            return {"content": [{"type": "text", "text": f"blot: {exc}"}], "isError": True}
        except Exception as exc:  # git missing, not a repository, a Blot bug: still a readable result
            traceback.print_exc(file=sys.stderr)
            return {"content": [{"type": "text", "text": f"blot: {exc.__class__.__name__}: {exc}"}], "isError": True}
        finally:
            if store is not None:
                store.close()
        return {"content": [{"type": "text", "text": text}], "structuredContent": data, "isError": False}


def _error(msg_id: Any, code: int, message: str, data: Any = None) -> dict:
    error: dict = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": error}


def serve(stdin: TextIO, stdout: TextIO, repo: str = ".") -> int:
    """Read messages until stdin closes. Each response is one line of JSON, flushed at once."""
    server = Server(repo)
    for line in stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            reply = _error(None, -32700, "Parse error")
        else:
            if isinstance(message, list):  # JSON-RPC batches: not part of MCP, answered one by one
                replies = [r for r in (server.handle(m) for m in message) if r is not None]
                reply = replies or None
            else:
                reply = server.handle(message)
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False, separators=(",", ":")) + "\n")
            stdout.flush()
    return 0


def serve_stdio(repo: str = ".") -> int:
    """`blot mcp`: serve on the real stdin/stdout, as UTF-8 whatever the console code page."""
    import os

    from blot.git import run_git

    try:
        repo = run_git(repo, "rev-parse", "--show-toplevel").strip() or repo
    except Exception:  # not a git repo: keep serving, each tool call will say so
        print(f"blot mcp: {os.path.abspath(repo)} is not a git repository", file=sys.stderr)
    stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace")
    stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", newline="\n")
    print(f"blot mcp {__version__}: serving {repo}", file=sys.stderr)
    try:
        return serve(stdin, stdout, repo)
    except KeyboardInterrupt:
        return 0
