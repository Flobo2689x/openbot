"""Stands in for the `claude` CLI in tests, so CI needs no install and no account.

FAKE_CLAUDE_DIR holds script.json, a list with one step per invocation (the last one repeats):
  {"lines": [...stream-json lines...], "stderr": "...", "exit": 0, "hang": false}
Each call appends its argv, cwd, stdin, system prompt file content and environment names to calls.jsonl.
A hanging step starts a child that sleeps too and writes both pids to pids.txt, for the cancel tests.
`--version` answers with FAKE_CLAUDE_VERSION (default a current version) and is not a call.
`auth status` answers with the exit code in FAKE_CLAUDE_DIR/auth_exit.txt (default 0) and a JSON body
{"loggedIn": <code == 0>, "authMethod": "claudeai"}; not a call either.
A step's "mcp_calls" ([{"tool": ..., "args": {...}}]) are made against the server in --mcp-config, as the
real CLI would, before any output; each response (or error) goes to mcp.jsonl, with the config it read.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

home = Path(os.environ["FAKE_CLAUDE_DIR"])
if sys.argv[1:] == ["--version"]:
    # FAKE_CLAUDE_VERSION stands in for an older install; each call is counted in versions.txt.
    with (home / "versions.txt").open("a", encoding="utf-8") as f:
        f.write("x")
    print(os.environ.get("FAKE_CLAUDE_VERSION", "2.1.283 (Claude Code)"), flush=True)
    sys.exit(0)
if sys.argv[1:] == ["auth", "status"]:
    exit_file = home / "auth_exit.txt"
    code = int(exit_file.read_text().strip()) if exit_file.is_file() else 0
    print(json.dumps({"loggedIn": code == 0, "authMethod": "claudeai"}), flush=True)
    sys.exit(code)
calls = home / "calls.jsonl"
done = len(calls.read_text(encoding="utf-8").splitlines()) if calls.exists() else 0
steps = json.loads((home / "script.json").read_text(encoding="utf-8"))
step = steps[min(done, len(steps) - 1)]
argv = sys.argv[1:]
prompt = sys.stdin.buffer.read().decode("utf-8")
system_prompt = ""
if "--append-system-prompt-file" in argv:
    system_prompt = Path(argv[argv.index("--append-system-prompt-file") + 1]).read_text(encoding="utf-8")
with calls.open("a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": argv, "cwd": os.getcwd(), "stdin": prompt, "system_prompt": system_prompt,
                        "env": sorted(os.environ)}) + "\n")
if step.get("mcp_calls"):
    config = json.loads(Path(argv[argv.index("--mcp-config") + 1]).read_text(encoding="utf-8"))
    server = next(iter(config["mcpServers"].values()))
    with (home / "mcp.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"config": config}) + "\n")
        for n, call in enumerate(step["mcp_calls"]):
            body = {"jsonrpc": "2.0", "id": n + 1, "method": "tools/call",
                    "params": {"name": call["tool"], "arguments": call.get("args", {})}}
            req = urllib.request.Request(server["url"], data=json.dumps(body).encode("utf-8"), method="POST",
                                         headers={**server.get("headers", {}), "Content-Type": "application/json",
                                                  "Accept": "application/json, text/event-stream"})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    f.write(json.dumps({"status": r.status, "body": json.loads(r.read().decode("utf-8"))}) + "\n")
            except urllib.error.HTTPError as e:
                f.write(json.dumps({"status": e.code, "body": e.read().decode("utf-8", "replace")}) + "\n")
# Bytes, as the real CLI writes UTF-8 whatever the console code page is.
for line in step.get("lines", []):
    sys.stdout.buffer.write(line.rstrip("\n").encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()
if step.get("stderr"):
    sys.stderr.write(step["stderr"])
    sys.stderr.flush()
if step.get("hang"):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    (home / "pids.txt").write_text(f"{os.getpid()} {child.pid}", encoding="utf-8")
    time.sleep(600)
sys.exit(step.get("exit", 0))
