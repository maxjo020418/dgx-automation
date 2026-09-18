#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")
MODEL_KINDS = {"chat", "completion", "embedding"}
VLLM_TASKS = {
    "auto",
    "classify",
    "draft",
    "embed",
    "embedding",
    "generate",
    "reward",
    "score",
    "transcription",
}
VLLM_RUNNERS = {"auto", "draft", "generate", "pooling"}
VLLM_CONVERTS = {"auto", "classify", "embed", "none", "reward"}


class ValidationError(Exception):
    pass


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValidationError(f"{path.name}: expected a YAML mapping")
    return data


def enabled_models(models_doc: dict[str, Any]) -> list[dict[str, Any]]:
    models = models_doc.get("models", [])
    if not isinstance(models, list):
        raise ValidationError("models.yml: top-level 'models' must be a list")
    return [m for m in models if bool(m.get("enabled", True))]


def as_list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list) or not value:
        raise ValidationError(f"{field}: expected a non-empty list")
    return value


def as_mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError(f"{field}: expected a mapping")
    return value


def validate_optional_choice(
    model_id: str, field: str, value: Any, allowed_values: set[str]
) -> None:
    if value is None:
        return
    normalized = str(value)
    if normalized not in allowed_values:
        allowed = ", ".join(sorted(allowed_values))
        raise ValidationError(f"{model_id}: {field} must be one of: {allowed}")


def validate_engine_args(model_id: str, value: Any) -> None:
    if value is None:
        return
    engine_args = as_mapping(value, f"{model_id}: vllm.engine_args")
    for key in engine_args:
        normalized = str(key)
        if not normalized.strip():
            raise ValidationError(f"{model_id}: vllm.engine_args keys must be non-empty")
        if normalized.strip() in {"-", "--"}:
            raise ValidationError(f"{model_id}: invalid vllm.engine_args key: {normalized!r}")


def validate(stack: dict[str, Any], models_doc: dict[str, Any], root: Path = ROOT) -> list[str]:
    messages: list[str] = []
    models = enabled_models(models_doc)
    ids: set[str] = set()
    served_names: dict[str, str] = {}
    host_ports: dict[int, str] = {}
    gpu_budget: dict[str, list[tuple[str, float]]] = defaultdict(list)

    gpu_cfg = as_mapping(stack.get("gpu", {}), "stack.gpu")
    max_fraction = float(gpu_cfg.get("max_fraction_per_gpu", 1.0))
    validate_memory_fraction_budget = bool(gpu_cfg.get("validate_memory_fraction_budget", True))
    allowed_device_ids = set(str(v) for v in gpu_cfg.get("allowed_device_ids", []))

    services = as_mapping(stack.get("services", {}), "stack.services")
    reserved_ports: dict[int, str] = {}
    for service_name, service_cfg in services.items():
        if not isinstance(service_cfg, dict):
            continue
        if service_cfg.get("enabled", True) and service_cfg.get("expose_host", False):
            port = service_cfg.get("host_port")
            if port is not None:
                reserved_ports[int(port)] = f"service:{service_name}"

    for index, model in enumerate(models):
        prefix = f"models[{index}]"
        model_id = str(model.get("id", ""))
        if not model_id:
            raise ValidationError(f"{prefix}.id is required")
        if not ID_RE.match(model_id):
            raise ValidationError(f"{prefix}.id '{model_id}' must match {ID_RE.pattern}")
        if model_id in ids:
            raise ValidationError(f"duplicate model id: {model_id}")
        ids.add(model_id)

        if model.get("backend") != "vllm":
            raise ValidationError(f"{prefix}.backend must be 'vllm'")

        kind = str(model.get("kind", "chat"))
        if kind not in MODEL_KINDS:
            raise ValidationError(f"{model_id}: kind must be chat, completion, or embedding")

        env_vars = model.get("env", {})
        if env_vars is not None:
            env = as_mapping(env_vars, f"{prefix}.env")
            for key in env:
                if not str(key):
                    raise ValidationError(f"{model_id}: env keys must be non-empty")

        names = [str(v) for v in as_list(model.get("served_names"), f"{prefix}.served_names")]
        for name in names:
            if name in served_names:
                raise ValidationError(
                    f"served name '{name}' used by both {served_names[name]} and {model_id}"
                )
            served_names[name] = model_id

        gpu = as_mapping(model.get("gpu"), f"{prefix}.gpu")
        device_ids = [str(v) for v in as_list(gpu.get("device_ids"), f"{prefix}.gpu.device_ids")]
        unknown = sorted(set(device_ids) - allowed_device_ids)
        if allowed_device_ids and unknown:
            raise ValidationError(f"{model_id}: unknown GPU device_ids: {', '.join(unknown)}")

        memory_fraction = float(gpu.get("memory_fraction", 0))
        if memory_fraction <= 0 or memory_fraction > 1:
            raise ValidationError(f"{model_id}: gpu.memory_fraction must be > 0 and <= 1")
        for device_id in device_ids:
            gpu_budget[device_id].append((model_id, memory_fraction))

        vllm = as_mapping(model.get("vllm", {}), f"{prefix}.vllm")
        recipe = vllm.get("recipe")
        if recipe is not None and not str(recipe).strip():
            raise ValidationError(f"{model_id}: vllm.recipe must be a non-empty string")
        validate_optional_choice(model_id, "vllm.task", vllm.get("task"), VLLM_TASKS)
        validate_optional_choice(model_id, "vllm.runner", vllm.get("runner"), VLLM_RUNNERS)
        validate_optional_choice(model_id, "vllm.convert", vllm.get("convert"), VLLM_CONVERTS)
        validate_engine_args(model_id, vllm.get("engine_args"))
        if kind == "embedding" and not any(field in vllm for field in ("task", "runner", "convert")):
            messages.append(
                f"WARN: {model_id}: embedding model relies on vLLM auto-detection; "
                "set vllm.runner=pooling and vllm.convert=embed, or vllm.task=embed "
                "for vLLM versions that require an explicit embedding mode"
            )
        for integer_field in ("max_model_len", "max_num_seqs", "max_num_batched_tokens"):
            if integer_field in vllm and int(vllm[integer_field]) <= 0:
                raise ValidationError(f"{model_id}: vllm.{integer_field} must be positive")
        tensor_parallel_size = int(vllm.get("tensor_parallel_size", 1))
        pipeline_parallel_size = int(vllm.get("pipeline_parallel_size", 1))
        if tensor_parallel_size <= 0:
            raise ValidationError(f"{model_id}: vllm.tensor_parallel_size must be positive")
        if pipeline_parallel_size <= 0:
            raise ValidationError(f"{model_id}: vllm.pipeline_parallel_size must be positive")
        if tensor_parallel_size * pipeline_parallel_size > len(device_ids):
            raise ValidationError(
                f"{model_id}: tensor_parallel_size * pipeline_parallel_size cannot exceed "
                "assigned GPU count"
            )
        if "kv_cache_memory_bytes" in vllm and "memory_fraction" in gpu and not vllm.get(
            "allow_fraction_with_kv_cache_memory_bytes", False
        ):
            raise ValidationError(
                f"{model_id}: do not set both gpu.memory_fraction and "
                "vllm.kv_cache_memory_bytes unless allow_fraction_with_kv_cache_memory_bytes=true"
            )
        chat_template = vllm.get("chat_template")
        if chat_template:
            template_path = root / str(chat_template)
            if not template_path.exists():
                raise ValidationError(f"{model_id}: chat template not found: {chat_template}")

        routing = as_mapping(model.get("routing"), f"{prefix}.routing")
        internal_port = int(routing.get("internal_port", 0))
        if internal_port <= 0:
            raise ValidationError(f"{model_id}: routing.internal_port must be positive")
        if routing.get("expose_publicly", False):
            raise ValidationError(f"{model_id}: raw vLLM should not expose_publicly")
        host_port = int(routing.get("host_port", 0))
        if host_port <= 0:
            raise ValidationError(f"{model_id}: routing.host_port is required for launch-cluster")
        if host_port in host_ports:
            raise ValidationError(
                f"host port {host_port} used by both {host_ports[host_port]} and {model_id}"
            )
        if routing.get("expose_host", False) and host_port in reserved_ports:
            raise ValidationError(
                f"host port {host_port} collides with {reserved_ports[host_port]}"
            )
        host_ports[host_port] = model_id

        litellm = as_mapping(model.get("litellm", {}), f"{prefix}.litellm")
        litellm_mode = str(litellm.get("mode", kind))
        if litellm_mode not in MODEL_KINDS:
            raise ValidationError(f"{model_id}: litellm.mode must be chat, embedding, or completion")
        if litellm_mode != kind:
            raise ValidationError(f"{model_id}: kind must match litellm.mode")
        litellm_params = litellm.get("params")
        if litellm_params is not None:
            params = as_mapping(litellm_params, f"{prefix}.litellm.params")
            reserved = {"api_base", "api_key", "extra_body", "model"} & set(str(k) for k in params)
            if reserved:
                reserved_list = ", ".join(sorted(reserved))
                raise ValidationError(
                    f"{model_id}: litellm.params cannot override rendered keys: {reserved_list}"
                )

    for device_id, entries in sorted(gpu_budget.items()):
        total = sum(fraction for _model_id, fraction in entries)
        if validate_memory_fraction_budget and total > max_fraction + 1e-9:
            detail = ", ".join(f"{model_id}={fraction:.3g}" for model_id, fraction in entries)
            raise ValidationError(
                f"GPU {device_id} memory budget {total:.3g} exceeds {max_fraction:.3g}: {detail}"
            )
        if validate_memory_fraction_budget:
            messages.append(f"OK: GPU {device_id} memory budget = {total:.3g} / {max_fraction:.3g}")
        else:
            messages.append(
                f"WARN: GPU {device_id} memory budget validation disabled "
                f"(configured total = {total:.3g} / {max_fraction:.3g})"
            )

    messages.insert(0, f"OK: {len(models)} enabled model(s)")
    messages.append("OK: unique model ids")
    messages.append("OK: unique served names")
    messages.append("OK: host ports are WireGuard-bound or internal-only")
    return messages


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stack", default=ROOT / "stack.yml", type=Path)
    parser.add_argument("--models", default=ROOT / "models.yml", type=Path)
    args = parser.parse_args()

    try:
        messages = validate(load_yaml(args.stack), load_yaml(args.models), ROOT)
    except ValidationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print("\n".join(messages))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
