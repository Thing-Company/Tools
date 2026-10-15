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
import time
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
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

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 3456

KNOWN_MODELS = {
    "kimi-k2.6": "kimi-k2.6",
    "kimi-k3": "kimi-k3",
    "deepseek-v4-pro": "deepseek-v4-pro",
    "grok-4.7": "grok-4.7",
    "grok-4.6": "grok-4.6",
    "claude-opus-5-5": "claude-opus-5-5",
    "claude-sonnet-5-5": "claude-sonnet-5-5",
    "claude-haiku-5-5": "claude-haiku-5-5",
    "gpt-5-3-codex": "gpt-5-3-codex",
    "gpt-5-2-codex": "gpt-5-2-codex",
    "gpt-5-1-codex": "gpt-5-1-codex",
}


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

    api_key = input("OpenCode Go API key: ").strip()
    host = input(f"Proxy host [{default_host}]: ").strip() or default_host
    port_str = input(f"Proxy port [{default_port}]: ").strip()
    port = int(port_str) if port_str else default_port

    if not api_key:
        print("Error: API key cannot be empty")
        sys.exit(1)

    config = {"api_key": api_key, "host": host, "port": port}
    save_config(config)
    print(f"\nConfig saved to {CONFIG_FILE}")
    print(f"Proxy will run on http://{host}:{port}")


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
        def log_message(self, format, *args):
            pass

        def do_GET(self):
            if self.path == "/health":
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"ok\n")
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length else b"{}"

            try:
                data = json.loads(body) if body else {}
            except json.JSONDecodeError:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'{"error": "invalid JSON"}')
                return

            path = self.path

            if path == "/v1/messages":
                handle_anthropic_messages(self, data, client, api_key)
            elif path == "/v1/chat/completions":
                handle_openai_chat(self, data, client, api_key)
            elif path == "/v1/responses":
                handle_openai_responses(self, data, client, api_key)
            elif path == "/v1/messages/count_tokens":
                handle_count_tokens(self, data)
            else:
                self.send_response(404)
                self.end_headers()

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
                content_str = " ".join(text_parts)
                msg_dict = {"role": role, "content": content_str}
                if tool_calls:
                    msg_dict["tool_calls"] = tool_calls
                openai_messages.append(msg_dict)
            else:
                openai_messages.append({"role": role, "content": str(content)})

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
        }

        try:
            resp = client.post(
                OPENCODE_GO_OPENAI_CHAT_URL,
                headers=headers,
                content=json.dumps(openai_data),
            )

            if resp.status_code >= 400:
                handler.send_response(resp.status_code)
                handler.end_headers()
                handler.wfile.write(resp.content)
                return

            if stream:
                handle_stream_chat_completion(handler, resp)
            else:
                handle_non_stream_chat_completion(handler, resp, model)
        except Exception as e:
            handler.send_response(502)
            handler.end_headers()
            handler.wfile.write(json.dumps({"error": str(e)}).encode())

    def handle_stream_chat_completion(handler, resp):
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.end_headers()

        buffer = b""
        for chunk in resp.iter_bytes():
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                line_str = line.decode("utf-8", errors="replace").strip()
                if not line_str or not line_str.startswith("data:"):
                    continue
                data_str = line_str[5:].strip()
                if data_str == "[DONE]":
                    handler.wfile.write(b'{"done": true}\n')
                    handler.wfile.flush()
                    continue
                try:
                    parsed = json.loads(data_str)
                    handler.wfile.write(json.dumps(parsed).encode() + b"\n")
                    handler.wfile.flush()
                except json.JSONDecodeError:
                    pass
        resp.close()

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
        }

        try:
            resp = client.post(
                OPENCODE_GO_OPENAI_CHAT_URL,
                headers=headers,
                content=json.dumps(data),
            )

            handler.send_response(resp.status_code)
            handler.send_header("Content-Type", "application/json")
            handler.end_headers()
            handler.wfile.write(resp.content)
        except Exception as e:
            handler.send_response(502)
            handler.end_headers()
            handler.wfile.write(json.dumps({"error": str(e)}).encode())

    def handle_openai_responses(handler, data, client, api_key):
        model = data.get("model", "")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        try:
            resp = client.post(
                OPENCODE_GO_OPENAI_RESPONSES_URL,
                headers=headers,
                content=json.dumps(data),
            )

            handler.send_response(resp.status_code)
            handler.send_header("Content-Type", "application/json")
            handler.end_headers()
            handler.wfile.write(resp.content)
        except Exception as e:
            handler.send_response(502)
            handler.end_headers()
            handler.wfile.write(json.dumps({"error": str(e)}).encode())

    def handle_count_tokens(handler, data):
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.end_headers()
        handler.wfile.write(json.dumps({"input_tokens": 0}).encode())

    server = HTTPServer((host, port), ProxyHandler)
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

    if model:
        env["ANTHROPIC_MODEL"] = model
        env["ANTHROPIC_SMALL_FAST_MODEL"] = model

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
    if model:
        print(f"Model: {model}")

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
    env["OPENAI_API_KEY"] = "ocgo-bridge"
    env["OPENAI_BASE_URL"] = base_url

    codex_path = subprocess.run(["which", "codex"], capture_output=True, text=True)
    if codex_path.returncode != 0:
        print("Error: 'codex' CLI not found in PATH")
        print("Install it with: npm install -g @openai/codex")
        sys.exit(1)

    args = ["codex"]
    if model:
        args.extend(["--model", model])
    if extra_args:
        args.extend(extra_args)

    print(f"Launching Codex CLI via OpenCode Go bridge...")
    print(f"Proxy: {base_url}")
    if model:
        print(f"Model: {model}")

    try:
        subprocess.run(args, env=env)
    except KeyboardInterrupt:
        pass


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
