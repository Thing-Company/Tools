#!/usr/bin/env python3
"""
OpenCode Go Bridge - Use your OpenCode Go subscription with Claude Code or Codex CLI.

A lightweight local proxy that translates Anthropic Messages API and OpenAI
Chat Completions API requests into OpenCode Go API calls.
"""

import json
import os
import sys
import subprocess
import signal
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import httpx
import yaml

CONFIG_DIR = Path.home() / ".config" / "opencode-go-bridge"
CONFIG_FILE = CONFIG_DIR / "config.json"
PID_FILE = CONFIG_DIR / "proxy.pid"
LOG_FILE = CONFIG_DIR / "proxy.log"

OPENCODE_GO_ANTHROPIC_URL = "https://opencode.ai/inference/go/anthropic/v1/messages"
OPENCODE_GO_OPENAI_CHAT_URL = "https://opencode.ai/inference/go/openai/v1/chat/completions"
OPENCODE_GO_OPENAI_RESPONSES_URL = "https://opencode.ai/inference/go/openai/v1/responses"
OPENCODE_GO_MODELS_URL = "https://opencode.ai/inference/go/v1/models"

SESSION_ID = str(uuid.uuid4())

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 3456

# Claude Code asks for an opus, a sonnet and a haiku model by default, none of
# which exist on Go. Every slot has to point at a model that does, or the run
# fails on the model check before a request is sent.
#
# The two tools cannot share one default: Codex speaks only the Responses API,
# and only a subset of Go models serve it, so they get separate defaults.
DEFAULT_MODEL = "longcat-2.0"
DEFAULT_CODEX_MODEL = "grok-4.7"

# <model-id>: (label, verified path)
KNOWN_MODELS = {
    "longcat-2.0": "longcat-2.0",
    "longcat-2.5-preview-free": "longcat-2.5-preview-free (free tier)",
    "mimo-v2.6-flash": "mimo-v2.6-flash (cheapest)",
    "glm-5.3-flash": "glm-5.3-flash (fastest)",
    "kimi-k2.7-code": "kimi-k2.7-code",
    "minimax-m2.5": "minimax-m2.5",
    "kimi-k2.6": "kimi-k2.6 (expensive reasoning)",
    "kimi-k3": "kimi-k3 (expensive reasoning)",
    "grok-4.7": "grok-4.7 (Responses API / Codex)",
    "deepseek-v4-pro": "deepseek-v4-pro (needs Global region)",
    "deepseek-v4.1-flash": "deepseek-v4.1-flash (needs Global region)",
}


def normalize_openai_messages(messages):
    """Fold stray system messages into the lead system message and drop empties.

    Claude Code injects mid-conversation system reminders, and most OpenAI-
    compatible providers reject a system role anywhere but index 0.
    """
    system_parts = []
    kept = []

    for msg in messages:
        role = msg.get("role")
        if role == "system":
            content = msg.get("content")
            if content:
                if isinstance(content, list):
                    system_parts.append(" ".join(
                        str(p.get("text", "")) for p in content
                        if isinstance(p, dict)))
                else:
                    system_parts.append(str(content))
            continue

        kept.append(msg)

    if system_parts:
        messages[:] = [{"role": "system", "content": "\n\n".join(system_parts)}] + kept
    else:
        messages[:] = kept

    # A conversation cannot open on a tool result.
    while messages and messages[0].get("role") == "tool":
        messages.pop(0)


def load_config():
    if not CONFIG_FILE.exists():
        return None
    with open(CONFIG_FILE) as f:
        return json.load(f)


def save_config(config):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def setup():
    print("OpenCode Go Bridge - Setup")
    print("Get your API key from https://opencode.ai/console/go")
    print()

    existing = load_config() or {}
    default_port = existing.get("port", DEFAULT_PORT)
    default_host = existing.get("host", DEFAULT_HOST)
    default_model = existing.get("model", DEFAULT_MODEL)

    api_key = input("OpenCode Go API key: ").strip()
    host = input(f"Proxy host [{default_host}]: ").strip() or default_host
    port_str = input(f"Proxy port [{default_port}]: ").strip()
    port = int(port_str) if port_str else default_port
    model = input(f"Default Claude model [{default_model}]: ").strip() or default_model
    default_codex_model = existing.get("codex_model", DEFAULT_CODEX_MODEL)
    codex_model = input(
        f"Default Codex model [{default_codex_model}]: "
    ).strip() or default_codex_model

    if not api_key:
        print("Error: API key cannot be empty")
        sys.exit(1)

    config = {
        "api_key": api_key,
        "host": host,
        "port": port,
        "model": model,
        "codex_model": codex_model,
    }
    save_config(config)
    print(f"\nConfig saved to {CONFIG_FILE}")
    print(f"Proxy will run on http://{host}:{port}")
    print(f"Claude model: {model}")
    print(f"Codex model:  {codex_model}")


def serve(background=False):
    config = load_config()
    if not config:
        print("Error: Not configured. Run 'setup' first.")
        sys.exit(1)

    if background:
        if PID_FILE.exists():
            try:
                old_pid = int(PID_FILE.read_text().strip())
                os.kill(old_pid, 0)
                print(f"Proxy already running (PID {old_pid})")
                return
            except (ProcessLookupError, ValueError):
                pass

        log = open(LOG_FILE, "a")
        proc = subprocess.Popen(
            [sys.executable, __file__, "serve"],
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        PID_FILE.write_text(str(proc.pid))
        print(f"Proxy started in background (PID {proc.pid})")
        print(f"Logs: {LOG_FILE}")
        return

    run_server(config)


def stop():
    if not PID_FILE.exists():
        print("Proxy is not running")
        return

    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        PID_FILE.unlink()
        print(f"Proxy stopped (PID {pid})")
    except ProcessLookupError:
        PID_FILE.unlink()
        print("Proxy was not running (stale PID file removed)")
    except Exception as e:
        print(f"Error stopping proxy: {e}")


def status():
    if not PID_FILE.exists():
        print("Proxy is not running")
        return

    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
        print(f"Proxy is running (PID {pid})")
    except (ProcessLookupError, ValueError):
        print("Proxy is not running (stale PID file)")


def list_models():
    print("Known OpenCode Go models:")
    print()
    for model_id, description in KNOWN_MODELS.items():
        print(f"  {model_id:<25} {description}")
    print()
    print("Fetch full list from API:")
    config = load_config()
    if config:
        try:
            resp = httpx.get(
                OPENCODE_GO_MODELS_URL,
                headers={"Authorization": f"Bearer {config['api_key']}"},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                if "data" in data:
                    print("\nAvailable models from API:")
                    for m in data["data"]:
                        print(f"  {m.get('id', 'unknown')}")
        except Exception as e:
            print(f"  Could not fetch models: {e}")


def run_server(config):
    host = config["host"]
    port = config["port"]
    api_key = config["api_key"]

    print(f"Starting proxy on http://{host}:{port}")

    client = httpx.Client(timeout=httpx.Timeout(300.0))

    class ProxyHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def send_sse_headers(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

        def write_sse(self, event, payload):
            body = f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode()
            self.wfile.write(f"{len(body):x}\r\n".encode())
            self.wfile.write(body + b"\r\n")
            self.wfile.flush()

        def end_sse(self):
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

        def send_json(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def send_raw(self, status, body, content_type="application/json"):
            if isinstance(body, str):
                body = body.encode()
            self.send_response(status)
            if content_type:
                self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

        def do_GET(self):
            if self.path == "/health":
                self.send_raw(200, b"ok\n", "text/plain")
            else:
                self.send_json(404, {"error": "not found"})

        def do_POST(self):
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length else b"{}"

            try:
                data = json.loads(body) if body else {}
            except json.JSONDecodeError:
                self.send_json(400, {"error": "invalid JSON"})
                return

            path = self.path

            route = path.split("?", 1)[0]

            if route == "/v1/messages":
                handle_anthropic_messages(self, data, client, api_key)
            elif route == "/v1/chat/completions":
                handle_openai_chat(self, data, client, api_key)
            elif route == "/v1/responses":
                handle_openai_responses(self, data, client, api_key)
            elif route == "/v1/messages/count_tokens":
                handle_count_tokens(self, data)
            else:
                self.send_json(404, {"error": "not found"})

    def handle_anthropic_messages(handler, data, client, api_key):
        model = data.get("model", "")
        messages = data.get("messages", [])
        system = data.get("system", "")
        max_tokens = data.get("max_tokens", 4096)
        stream = data.get("stream", False)
        tools = data.get("tools", [])

        openai_messages = []
        if system:
            if isinstance(system, list):
                system_text = " ".join(
                    s.get("text", "") for s in system if isinstance(s, dict)
                )
            else:
                system_text = system
            openai_messages.append({"role": "system", "content": system_text})

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if isinstance(content, list):
                text_parts = []
                tool_calls = []
                for part in content:
                    if isinstance(part, dict):
                        if part.get("type") == "text":
                            text_parts.append(part.get("text", ""))
                        elif part.get("type") == "tool_use":
                            tool_calls.append({
                                "id": part.get("id", ""),
                                "type": "function",
                                "function": {
                                    "name": part.get("name", ""),
                                    "arguments": json.dumps(part.get("input", {})),
                                },
                            })
                        elif part.get("type") == "tool_result":
                            tool_call_id = part.get("tool_use_id", "")
                            result_content = part.get("content", "")
                            if isinstance(result_content, list):
                                result_content = " ".join(
                                    p.get("text", "") for p in result_content
                                    if isinstance(p, dict)
                                )
                            openai_messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call_id,
                                "content": str(result_content),
                            })
                content_str = " ".join(text_parts).strip()
                if tool_calls and not content_str:
                    msg_dict = {"role": role, "content": None, "tool_calls": tool_calls}
                    openai_messages.append(msg_dict)
                elif tool_calls or content_str:
                    msg_dict = {"role": role, "content": content_str}
                    if tool_calls:
                        msg_dict["tool_calls"] = tool_calls
                    openai_messages.append(msg_dict)
                # A message holding only tool_results already produced its own
                # "tool" entries; emitting an empty user turn upsets providers.
            else:
                text = str(content).strip()
                if text:
                    openai_messages.append({"role": role, "content": text})

        normalize_openai_messages(openai_messages)

        openai_tools = []
        for tool in tools:
            if isinstance(tool, dict):
                openai_tools.append({
                    "type": "function",
                    "function": {
                        "name": tool.get("name", ""),
                        "description": tool.get("description", ""),
                        "parameters": tool.get("input_schema", {}),
                    },
                })

        openai_data = {
            "model": model,
            "messages": openai_messages,
            "max_tokens": max_tokens,
            "stream": stream,
        }
        if openai_tools:
            openai_data["tools"] = openai_tools

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "x-opencode-session": SESSION_ID,
        }

        try:
            resp = client.post(
                OPENCODE_GO_OPENAI_CHAT_URL,
                headers=headers,
                content=json.dumps(openai_data),
            )

            if resp.status_code >= 400:
                handler.send_raw(resp.status_code, resp.content)
                return

            if stream:
                handle_stream_chat_completion(handler, resp, model)
            else:
                handle_non_stream_chat_completion(handler, resp, model)
        except Exception as e:
            handler.send_response(502)
            handler.end_headers()
            handler.wfile.write(json.dumps({"error": str(e)}).encode())

    def handle_stream_chat_completion(handler, resp, model):
        try:
            handler.send_sse_headers()
        except (BrokenPipeError, ConnectionResetError):
            resp.close()
            return

        message_id = f"msg_{uuid.uuid4().hex[:24]}"

        def sse(event, payload):
            handler.write_sse(event, payload)

        started = False
        stop_reason = "end_turn"
        usage = {"input_tokens": 0, "output_tokens": 0}
        text_open = False
        text_index = 0
        tools = {}
        next_index = 1

        def close_text_block():
            nonlocal text_open
            if text_open:
                sse("content_block_stop", {"type": "content_block_stop", "index": text_index})
                text_open = False

        try:
            buffer = b""
            for chunk in resp.iter_bytes():
                buffer += chunk
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        continue

                    try:
                        parsed = json.loads(payload)
                    except json.JSONDecodeError:
                        continue

                    if not started:
                        started = True
                        sse("message_start", {
                            "type": "message_start",
                            "message": {
                                "id": message_id,
                                "type": "message",
                                "role": "assistant",
                                "content": [],
                                "model": model,
                                "stop_reason": None,
                                "stop_sequence": None,
                                "usage": {"input_tokens": 0, "output_tokens": 0},
                            },
                        })

                    u = parsed.get("usage") or {}
                    if u.get("prompt_tokens") is not None:
                        usage["input_tokens"] = u["prompt_tokens"]
                    if u.get("completion_tokens") is not None:
                        usage["output_tokens"] = u["completion_tokens"]

                    choices = parsed.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0]
                    delta = choice.get("delta") or {}

                    text = delta.get("content")
                    if text:
                        if not text_open:
                            close_text_block()
                            sse("content_block_start", {
                                "type": "content_block_start",
                                "index": text_index,
                                "content_block": {"type": "text", "text": ""},
                            })
                            text_open = True
                        sse("content_block_delta", {
                            "type": "content_block_delta",
                            "index": text_index,
                            "delta": {"type": "text_delta", "text": text},
                        })

                    for tc in delta.get("tool_calls") or []:
                        idx = tc.get("index", 0)
                        if idx not in tools:
                            close_text_block()
                            fn = tc.get("function") or {}
                            tools[idx] = {
                                "block": next_index,
                                "id": tc.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
                                "name": fn.get("name") or "",
                                "args": "",
                            }
                            next_index += 1
                            sse("content_block_start", {
                                "type": "content_block_start",
                                "index": tools[idx]["block"],
                                "content_block": {
                                    "type": "tool_use",
                                    "id": tools[idx]["id"],
                                    "name": tools[idx]["name"],
                                    "input": {},
                                },
                            })
                        frag = (tc.get("function") or {}).get("arguments")
                        if frag:
                            tools[idx]["args"] += frag
                            sse("content_block_delta", {
                                "type": "content_block_delta",
                                "index": tools[idx]["block"],
                                "delta": {"type": "input_json_delta", "partial_json": frag},
                            })

                    fr = choice.get("finish_reason")
                    if fr:
                        if fr == "length":
                            stop_reason = "max_tokens"
                        elif fr == "tool_calls":
                            stop_reason = "tool_use"
                        else:
                            stop_reason = "end_turn"
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            resp.close()

        if started:
            close_text_block()
            for idx in sorted(tools):
                sse("content_block_stop", {
                    "type": "content_block_stop",
                    "index": tools[idx]["block"],
                })
            sse("message_delta", {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                "usage": {"output_tokens": usage["output_tokens"]},
            })
            sse("message_stop", {"type": "message_stop"})

        try:
            handler.end_sse()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def handle_non_stream_chat_completion(handler, resp, model):
        try:
            result = resp.json()
        except Exception:
            handler.send_response(502)
            handler.end_headers()
            handler.wfile.write(b'{"error": "invalid upstream response"}')
            return

        choices = result.get("choices", [])
        if choices:
            choice = choices[0]
            msg = choice.get("message", {})
            content = msg.get("content", "")
            tool_calls = msg.get("tool_calls", [])

            anthropic_content = []
            if content:
                anthropic_content.append({"type": "text", "text": content})

            for tc in tool_calls:
                func = tc.get("function", {})
                anthropic_content.append({
                    "type": "tool_use",
                    "id": tc.get("id", ""),
                    "name": func.get("name", ""),
                    "input": json.loads(func.get("arguments", "{}")),
                })

            anthropic_resp = {
                "id": result.get("id", ""),
                "type": "message",
                "role": "assistant",
                "content": anthropic_content,
                "model": model,
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {
                    "input_tokens": result.get("usage", {}).get("prompt_tokens", 0),
                    "output_tokens": result.get("usage", {}).get("completion_tokens", 0),
                },
            }
        else:
            anthropic_resp = {
                "id": result.get("id", ""),
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": ""}],
                "model": model,
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            }

        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.end_headers()
        handler.wfile.write(json.dumps(anthropic_resp).encode())

    def handle_openai_chat(handler, data, client, api_key):
        model = data.get("model", "")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "x-opencode-session": SESSION_ID,
        }

        try:
            resp = client.post(
                OPENCODE_GO_OPENAI_CHAT_URL,
                headers=headers,
                content=json.dumps(data),
            )

            handler.send_raw(resp.status_code, resp.content)
        except Exception as e:
            handler.send_json(502, {"error": str(e)})

    def sanitize_responses_body(payload):
        data = dict(payload)

        tools = data.get("tools")
        if isinstance(tools, list):
            cleaned = []
            for t in tools:
                if not isinstance(t, dict):
                    continue
                if t.get("type") == "namespace":
                    # OpenCode Go's Responses endpoint rejects namespace tool
                    # containers; flatten their members to plain functions.
                    inner = t.get("tools")
                    if isinstance(inner, list):
                        for it in inner:
                            if isinstance(it, dict):
                                cleaned.append(
                                    {k: v for k, v in it.items()
                                     if k != "external_web_access"}
                                )
                    continue
                cleaned.append(
                    {k: v for k, v in t.items() if k != "external_web_access"}
                )
            data["tools"] = cleaned

        if not data.get("tools") and "tool_choice" in data:
            data.pop("tool_choice", None)

        if data.get("instructions") == "":
            data.pop("instructions", None)

        return data

    def handle_openai_responses(handler, data, client, api_key):
        data = sanitize_responses_body(data)
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "x-opencode-session": SESSION_ID,
        }

        try:
            resp = client.post(
                OPENCODE_GO_OPENAI_RESPONSES_URL,
                headers=headers,
                content=json.dumps(data),
            )

            handler.send_raw(resp.status_code, resp.content)
        except Exception as e:
            handler.send_json(502, {"error": str(e)})

    def handle_count_tokens(handler, data):
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.end_headers()
        handler.wfile.write(json.dumps({"input_tokens": 0}).encode())

    server = ThreadingHTTPServer((host, port), ProxyHandler)
    print(f"Proxy ready. Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nProxy stopped.")
    finally:
        client.close()


def launch_claude(model=None, extra_args=None):
    config = load_config()
    if not config:
        print("Error: Not configured. Run 'setup' first.")
        sys.exit(1)

    base_url = f"http://{config['host']}:{config['port']}"

    if not PID_FILE.exists():
        print("Starting proxy...")
        proc = subprocess.Popen(
            [sys.executable, __file__, "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        PID_FILE.write_text(str(proc.pid))
        time.sleep(1)

    env = os.environ.copy()
    env["ANTHROPIC_BASE_URL"] = base_url
    env["ANTHROPIC_AUTH_TOKEN"] = "unused"
    env["ANTHROPIC_API_KEY"] = "unused"

    chosen = model or config.get("model") or DEFAULT_MODEL

    # Claude Code reads a different slot depending on the job: ANTHROPIC_MODEL
    # for the main turn, the DEFAULT_* trio for its opus/sonnet/haiku picker,
    # and SMALL_FAST_MODEL for background work like titles. Leaving any of them
    # unset makes it ask for a real Claude model and die on the model check.
    slots = [
        "ANTHROPIC_MODEL",
        "ANTHROPIC_SMALL_FAST_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    ]
    for slot in slots:
        env[slot] = chosen

    claude_path = subprocess.run(["which", "claude"], capture_output=True, text=True)
    if claude_path.returncode != 0:
        print("Error: 'claude' CLI not found in PATH")
        print("Install it with: npm install -g @anthropic-ai/claude-code")
        sys.exit(1)

    args = ["claude"]
    if extra_args:
        args.extend(extra_args)

    print(f"Launching Claude Code via OpenCode Go bridge...")
    print(f"Proxy: {base_url}")
    print(f"Model: {chosen}")

    try:
        subprocess.run(args, env=env)
    except KeyboardInterrupt:
        pass


def launch_codex(model=None, extra_args=None):
    config = load_config()
    if not config:
        print("Error: Not configured. Run 'setup' first.")
        sys.exit(1)

    base_url = f"http://{config['host']}:{config['port']}"

    if not PID_FILE.exists():
        print("Starting proxy...")
        proc = subprocess.Popen(
            [sys.executable, __file__, "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        PID_FILE.write_text(str(proc.pid))
        time.sleep(1)

    env = os.environ.copy()
    # Codex only honors an API key, never OPENAI_BASE_URL, for the built-in
    # provider, so the endpoint has to arrive as provider config. Passing it as
    # -c flags keeps it scoped to this launch, the way the standardcompute
    # launcher does, and leaves ~/.codex untouched. An empty CODEX_HOME is what
    # stops a stored ChatGPT login from overriding the key and tripping Codex's
    # own client-side model validation.
    env["OPENAI_API_KEY"] = "ocgo-bridge"
    chosen = model or config.get("codex_model") or DEFAULT_CODEX_MODEL
    codex_home = Path(tempfile.mkdtemp(prefix="opencode-go-codex-"))
    env["CODEX_HOME"] = str(codex_home)

    provider = [
        "-c", f'model="{chosen}"',
        "-c", 'model_provider="opencode-go"',
        "-c", 'model_providers.opencode-go.name="OpenCode Go"',
        "-c", f'model_providers.opencode-go.base_url="{base_url}/v1"',
        "-c", 'model_providers.opencode-go.env_key="OPENAI_API_KEY"',
        "-c", 'model_providers.opencode-go.wire_api="responses"',
    ]

    codex_path = subprocess.run(["which", "codex"], capture_output=True, text=True)
    if codex_path.returncode != 0:
        print("Error: 'codex' CLI not found in PATH")
        print("Install it with: npm install -g @openai/codex")
        shutil.rmtree(codex_home, ignore_errors=True)
        sys.exit(1)

    args = ["codex"] + provider
    if extra_args:
        args.extend(extra_args)

    print(f"Launching Codex CLI via OpenCode Go bridge...")
    print(f"Proxy: {base_url}")
    print(f"Model: {chosen}")

    try:
        subprocess.run(args, env=env)
    except KeyboardInterrupt:
        pass
    finally:
        shutil.rmtree(codex_home, ignore_errors=True)


def main():
    if len(sys.argv) < 2:
        print("OpenCode Go Bridge - Use your OpenCode Go subscription with Claude Code or Codex CLI")
        print()
        print("Commands:")
        print("  setup              Configure your API key")
        print("  serve              Start the proxy server")
        print("  serve --background Start proxy in background")
        print("  stop               Stop background proxy")
        print("  status             Check proxy status")
        print("  list               List known models")
        print("  launch claude      Start Claude Code")
        print("  launch codex       Start Codex CLI")
        print()
        print(f"Config: {CONFIG_FILE}")
        return

    cmd = sys.argv[1]

    if cmd == "setup":
        setup()
    elif cmd == "serve":
        serve(background="--background" in sys.argv)
    elif cmd == "stop":
        stop()
    elif cmd == "status":
        status()
    elif cmd == "list":
        list_models()
    elif cmd == "launch":
        if len(sys.argv) < 3:
            print("Usage: launch claude|codex [--model MODEL] [extra args...]")
            sys.exit(1)
        tool = sys.argv[2]
        model = None
        extra_args = []
        args = sys.argv[3:]
        i = 0
        while i < len(args):
            if args[i] == "--":
                i += 1
                extra_args.extend(args[i:])
                break
            if args[i] == "--model" and i + 1 < len(args):
                model = args[i + 1]
                i += 2
            else:
                extra_args.append(args[i])
                i += 1

        if tool == "claude":
            launch_claude(model, extra_args)
        elif tool == "codex":
            launch_codex(model, extra_args)
        else:
            print(f"Unknown tool: {tool}")
            sys.exit(1)
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
