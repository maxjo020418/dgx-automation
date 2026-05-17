#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--stack", default=ROOT / "stack.yml", type=Path)
    parser.add_argument("--models", default=ROOT / "models.yml", type=Path)
    args = parser.parse_args()

    stack = yaml.safe_load(args.stack.read_text(encoding="utf-8"))
    models_doc = yaml.safe_load(args.models.read_text(encoding="utf-8"))
    max_fraction = float(stack["gpu"]["max_fraction_per_gpu"])
    headroom = float(stack["gpu"].get("default_headroom", 0))
    available = max_fraction - headroom

    groups: dict[str, list[tuple[dict[str, Any], float]]] = defaultdict(list)
    for model in models_doc.get("models", []):
        if not model.get("enabled", True):
            continue
        sizing = model.get("sizing", {})
        if sizing.get("mode") != "weighted":
            continue
        weight = float(sizing.get("weight", 1))
        for device_id in model["gpu"]["device_ids"]:
            groups[str(device_id)].append((model, weight))

    for device_id, entries in sorted(groups.items()):
        total_weight = sum(weight for _model, weight in entries)
        if total_weight <= 0:
            continue
        for model, weight in entries:
            sizing = model.get("sizing", {})
            fraction = available * weight / total_weight
            fraction = max(float(sizing.get("min_fraction", 0)), fraction)
            fraction = min(float(sizing.get("max_fraction", 1)), fraction)
            model["gpu"]["memory_fraction"] = round(fraction, 3)
            print(f"{device_id}: {model['id']} -> {model['gpu']['memory_fraction']}")

    if args.write:
        args.models.write_text(yaml.safe_dump(models_doc, sort_keys=False), encoding="utf-8")
    elif groups:
        print("dry run only; pass --write to update models.yml")
    else:
        print("no weighted sizing entries found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
