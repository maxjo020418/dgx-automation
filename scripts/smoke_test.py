#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]


def ssh(user: str, host: str, command: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["ssh", f"{user}@{host}", command],
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def remote_curl(user: str, host: str, url: str, headers: dict[str, str] | None = None, data: Any = None) -> tuple[int, str]:
    header_args = ""
    for key, value in (headers or {}).items():
        header_args += f" -H {json.dumps(f'{key}: {value}')}"
    data_arg = ""
    if data is not None:
        data_arg = " -d " + json.dumps(json.dumps(data))
    cmd = f"curl -fsS --max-time 20{header_args}{data_arg} {json.dumps(url)}"
    proc = ssh(user, host, cmd, timeout=35)
    return proc.returncode, proc.stdout + proc.stderr


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="spark.cg-rookies.net")
    parser.add_argument("--ssh-user", default="ymin")
    args = parser.parse_args()

    stack = yaml.safe_load((ROOT / "stack.yml").read_text(encoding="utf-8"))
    models_doc = yaml.safe_load((ROOT / "models.yml").read_text(encoding="utf-8"))
    bind_ip = stack["network"]["bind_ip"]
    litellm_auth_enabled = bool(stack["services"]["litellm"].get("auth_enabled", True))
    failures: list[str] = []

    master_key = ""
    if litellm_auth_enabled:
        env_proc = ssh(
            args.ssh_user,
            args.host,
            "sudo grep -E '^LITELLM_MASTER_KEY=' /opt/dgx-llm-stack/.env | cut -d= -f2-",
        )
        master_key = env_proc.stdout.strip()

    checks = {
        "OpenWebUI": (f"http://{bind_ip}:{stack['services']['openwebui']['host_port']}/health", None),
        "Grafana": (f"http://{bind_ip}:{stack['services']['grafana']['host_port']}/api/health", None),
        "SearXNG": (f"http://{bind_ip}:{stack['services']['searxng']['host_port']}/", None),
    }
    if litellm_auth_enabled and master_key:
        checks["LiteLLM models"] = (
            f"http://{bind_ip}:{stack['services']['litellm']['host_port']}/v1/models",
            {"Authorization": f"Bearer {master_key}"},
        )
    elif not litellm_auth_enabled:
        checks["LiteLLM models"] = (
            f"http://{bind_ip}:{stack['services']['litellm']['host_port']}/v1/models",
            None,
        )
    else:
        print("WARN: no LITELLM_MASTER_KEY found; skipped LiteLLM model list auth")

    for name, (url, headers) in checks.items():
        code, output = remote_curl(args.ssh_user, args.host, url, headers=headers)
        if code == 0:
            print(f"OK: {name}")
        else:
            print(f"ERROR: {name}: {output.strip()}", file=sys.stderr)
            failures.append(name)

    if master_key or not litellm_auth_enabled:
        for model in models_doc.get("models", []):
            if not model.get("enabled", True) or model.get("kind") != "chat":
                continue
            served_name = model["served_names"][0]
            payload = {
                "model": served_name,
                "messages": [{"role": "user", "content": "Say OK only."}],
                "max_tokens": 8,
            }
            extra_body = model.get("litellm", {}).get("extra_body")
            if extra_body:
                payload.update(extra_body)
            code, output = remote_curl(
                args.ssh_user,
                args.host,
                f"http://{bind_ip}:{stack['services']['litellm']['host_port']}/v1/chat/completions",
                headers={
                    **({"Authorization": f"Bearer {master_key}"} if master_key else {}),
                    "Content-Type": "application/json",
                },
                data=payload,
            )
            if code == 0:
                print(f"OK: chat completion {served_name}")
            else:
                print(f"ERROR: chat completion {served_name}: {output.strip()}", file=sys.stderr)
                failures.append(f"chat:{served_name}")
    else:
        print("WARN: no LITELLM_MASTER_KEY found; skipped model completion smoke")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
