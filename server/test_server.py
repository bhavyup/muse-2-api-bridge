#!/usr/bin/env python3
"""End-to-end tests for jarvis-serve/server/server.py. Stdlib only.

Spins up real server subprocesses, simulates the agent worker by writing
canned response files into the queue pipeline, and asserts the full HTTP
contract: auth, models, non-streaming, SSE streaming (text + tool calls),
timeouts, malformed agent output, and bad client input.

Usage:  python3 test_server.py
Exit code 0 = all green.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "server.py")
TOKEN = "test-token-abc123"
TOKEN_SHA = hashlib.sha256(TOKEN.encode()).hexdigest()

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def http(base, method, path, body=None, token=TOKEN, raw_body=None):
    url = base + path
    data = raw_body if raw_body is not None else (
        json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(url, data=data, method=method)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def sse_events(raw: bytes):
    """Parse SSE stream into list of data payloads (str)."""
    events = []
    for chunk in raw.decode().split("\n\n"):
        chunk = chunk.strip()
        if chunk.startswith("data:"):
            events.append(chunk[len("data:"):].strip())
    return events


class Server:
    def __init__(self, port: int, timeout_secs: float = 300):
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        self.root = tempfile.mkdtemp(prefix="js-test-")
        self.data = os.path.join(self.root, "data")
        cfg = {
            "bind": "127.0.0.1", "port": port, "token_sha256": TOKEN_SHA,
            "model": "muse-agent-test", "data_dir": self.data,
            "request_timeout_secs": timeout_secs,
            "log_file": os.path.join(self.root, "server.log"),
        }
        self.cfg_path = os.path.join(self.root, "config.json")
        with open(self.cfg_path, "w") as f:
            json.dump(cfg, f)
        self.proc = subprocess.Popen(
            [sys.executable, SERVER, "--config", self.cfg_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # wait for readiness
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            raise RuntimeError("server did not start")

    def close(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        shutil.rmtree(self.root, ignore_errors=True)


class AgentSimulator(threading.Thread):
    """Watches queue/ and answers the first new request with `canned`."""

    def __init__(self, data_dir: str, canned: dict, delay: float = 0.2):
        super().__init__(daemon=True)
        self.data_dir = data_dir
        self.canned = canned
        self.delay = delay
        self.seen: set[str] = set()
        self._stop_event = threading.Event()

    def run(self):
        q = os.path.join(self.data_dir, "queue")
        r = os.path.join(self.data_dir, "responses")
        while not self._stop_event.is_set():
            try:
                names = os.listdir(q)
            except OSError:
                names = []
            for n in names:
                if n in self.seen or not n.endswith(".json"):
                    continue
                self.seen.add(n)
                time.sleep(self.delay)
                tmp = os.path.join(r, n + ".tmp")
                with open(tmp, "w") as f:
                    json.dump(self.canned, f)
                os.rename(tmp, os.path.join(r, n))
            time.sleep(0.02)

    def stop(self):
        self._stop_event.set()


def with_agent(server: Server, canned: dict, delay: float = 0.2):
    sim = AgentSimulator(server.data, canned, delay)
    sim.start()
    return sim


def main() -> int:
    print("== jarvis-serve server tests ==")
    srv = Server(18765)

    # --- health & models ------------------------------------------------ #
    code, body = http(srv.base, "GET", "/healthz", token=None)
    check("healthz no-auth", code == 200 and json.loads(body)["status"] == "ok",
          f"{code} {body[:80]}")

    code, _ = http(srv.base, "GET", "/v1/models", token=None)
    check("models without token -> 401", code == 401, f"{code}")
    code, _ = http(srv.base, "GET", "/v1/models", token="wrong")
    check("models wrong token -> 401", code == 401, f"{code}")
    code, body = http(srv.base, "GET", "/v1/models")
    data = json.loads(body)
    check("models ok", code == 200 and data["data"][0]["id"] == "muse-agent-test",
          f"{code} {body[:80]}")

    code, _ = http(srv.base, "GET", "/nope")
    check("unknown path -> 404", code == 404, f"{code}")

    # --- bad client input ------------------------------------------------ #
    code, _ = http(srv.base, "POST", "/v1/chat/completions",
                   raw_body=b"{not json")
    check("malformed JSON -> 400", code == 400, f"{code}")
    code, _ = http(srv.base, "POST", "/v1/chat/completions", body={"model": "x"})
    check("missing messages -> 400", code == 400, f"{code}")
    code, _ = http(srv.base, "POST", "/v1/chat/completions",
                   body={"messages": []})
    check("empty messages -> 400", code == 400, f"{code}")

    payload = lambda **kw: {"model": "muse-agent-test",
                            "messages": [{"role": "user", "content": "hi"}],
                            **kw}

    # --- non-streaming text ---------------------------------------------- #
    sim = with_agent(srv, {"content": "hello there",
                           "usage": {"prompt_tokens": 10,
                                     "completion_tokens": 2}})
    code, body = http(srv.base, "POST", "/v1/chat/completions",
                      body=payload())
    sim.stop()
    data = json.loads(body)
    ch = data["choices"][0]
    check("non-stream 200", code == 200, f"{code}")
    check("non-stream envelope",
          data["object"] == "chat.completion"
          and data["id"].startswith("chatcmpl-req_")
          and ch["message"] == {"role": "assistant", "content": "hello there"}
          and ch["finish_reason"] == "stop"
          and data["usage"]["total_tokens"] == 12,
          body[:160])

    # --- non-streaming tool calls ----------------------------------------- #
    sim = with_agent(srv, {
        "content": None,
        "tool_calls": [{"id": "call_1", "name": "read_file",
                        "arguments": json.dumps({"path": "/tmp/x"})}],
        "finish_reason": "tool_calls"})
    code, body = http(srv.base, "POST", "/v1/chat/completions",
                      body=payload(tools=[{"type": "function",
                                          "function": {"name": "read_file"}}]))
    sim.stop()
    data = json.loads(body)
    tc = data["choices"][0]["message"]["tool_calls"][0]
    check("tool_calls passthrough",
          code == 200 and tc["id"] == "call_1"
          and tc["type"] == "function"
          and tc["function"]["name"] == "read_file"
          and json.loads(tc["function"]["arguments"]) == {"path": "/tmp/x"}
          and data["choices"][0]["finish_reason"] == "tool_calls",
          body[:200])

    # --- streaming text ---------------------------------------------------- #
    long_text = "The quick brown fox jumps over the lazy dog. " * 20
    sim = with_agent(srv, {"content": long_text})
    code, body = http(srv.base, "POST", "/v1/chat/completions",
                      body=payload(stream=True))
    sim.stop()
    events = sse_events(body)
    check("stream ends with [DONE]", code == 200 and events[-1] == "[DONE]",
          f"{code} last={events[-1] if events else None}")
    chunks = [json.loads(e) for e in events[:-1]]
    check("stream envelope",
          all(c["object"] == "chat.completion.chunk"
              and c["id"].startswith("chatcmpl-req_") for c in chunks))
    check("stream role first",
          chunks[0]["choices"][0]["delta"].get("role") == "assistant")
    reassembled = "".join(c["choices"][0]["delta"].get("content", "")
                          for c in chunks)
    check("stream content reassembles", reassembled == long_text,
          f"len {len(reassembled)} vs {len(long_text)}")
    check("stream finish_reason stop",
          chunks[-1]["choices"][0]["finish_reason"] == "stop")

    # --- streaming tool calls ---------------------------------------------- #
    args = json.dumps({"path": "/tmp/x", "mode": "r"})
    sim = with_agent(srv, {
        "content": "reading it",
        "tool_calls": [{"id": "call_9", "name": "read_file",
                        "arguments": args}]})
    code, body = http(srv.base, "POST", "/v1/chat/completions",
                      body=payload(stream=True))
    sim.stop()
    events = sse_events(body)
    chunks = [json.loads(e) for e in events[:-1]]
    tcs = {}
    for c in chunks:
        for tc in c["choices"][0]["delta"].get("tool_calls", []):
            i = tc["index"]
            tcs.setdefault(i, {"args": ""})
            if tc.get("id"):
                tcs[i]["id"] = tc["id"]
            fn = tc.get("function", {})
            if fn.get("name"):
                tcs[i]["name"] = fn["name"]
            tcs[i]["args"] += fn.get("arguments", "")
    check("stream tool_call reassembles",
          tcs[0]["id"] == "call_9" and tcs[0]["name"] == "read_file"
          and json.loads(tcs[0]["args"]) == {"path": "/tmp/x", "mode": "r"},
          str(tcs)[:160])
    check("stream finish_reason tool_calls",
          chunks[-1]["choices"][0]["finish_reason"] == "tool_calls")

    # --- agent contract violations -> 500, never a hang --------------------- #
    for bad, label in [
        ({"content": 123}, "non-string content"),
        ({"tool_calls": [{"id": "c", "name": "n",
                          "arguments": "{bad json"}]},
         "bad arguments JSON"),
        ({"finish_reason": "error", "error": "boom"}, "agent error"),
        (["not", "a", "dict"], "non-object"),
    ]:
        sim = with_agent(srv, bad)
        code, body = http(srv.base, "POST", "/v1/chat/completions",
                          body=payload())
        sim.stop()
        check(f"agent violation -> 500 ({label})",
              code == 500 and "error" in json.loads(body), f"{code}")

    srv.close()

    # --- timeout -> 504 ------------------------------------------------------ #
    srv2 = Server(18766, timeout_secs=2)
    t0 = time.time()
    code, body = http(srv2.base, "POST", "/v1/chat/completions",
                      body=payload())
    dt = time.time() - t0
    check("agent timeout -> 504", code == 504
          and json.loads(body)["error"]["code"] == "agent_timeout"
          and 1.5 < dt < 10, f"{code} dt={dt:.1f}s")
    srv2.close()

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
