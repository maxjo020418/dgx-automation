#!/usr/bin/env python3
import csv
import math
import os
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


FIELDS = [
    "index",
    "name",
    "uuid",
    "utilization.gpu",
    "temperature.gpu",
    "memory.total",
    "memory.used",
    "memory.free",
    "power.draw",
    "power.limit",
]

METRICS = {
    "utilization.gpu": ("nvidia_gpu_utilization_percent", "gauge", "GPU utilization percent.", 1.0),
    "temperature.gpu": ("nvidia_gpu_temperature_celsius", "gauge", "GPU temperature in Celsius.", 1.0),
    "memory.total": ("nvidia_gpu_memory_total_bytes", "gauge", "Total GPU memory in bytes.", 1024.0 * 1024.0),
    "memory.used": ("nvidia_gpu_memory_used_bytes", "gauge", "Used GPU memory in bytes.", 1024.0 * 1024.0),
    "memory.free": ("nvidia_gpu_memory_free_bytes", "gauge", "Free GPU memory in bytes.", 1024.0 * 1024.0),
    "power.draw": ("nvidia_gpu_power_draw_watts", "gauge", "GPU power draw in watts.", 1.0),
    "power.limit": ("nvidia_gpu_power_limit_watts", "gauge", "GPU power limit in watts.", 1.0),
}


def prom_escape(value):
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def parse_number(value):
    value = value.strip()
    if not value or value == "[N/A]" or value.upper() == "N/A":
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def collect():
    start = time.time()
    cmd = [
        "nvidia-smi",
        f"--query-gpu={','.join(FIELDS)}",
        "--format=csv,noheader,nounits",
    ]
    proc = subprocess.run(cmd, text=True, capture_output=True, timeout=5, check=False)
    lines = [
        "# HELP nvidia_smi_exporter_scrape_success Whether the last nvidia-smi scrape succeeded.",
        "# TYPE nvidia_smi_exporter_scrape_success gauge",
    ]
    success = 1 if proc.returncode == 0 else 0
    lines.append(f"nvidia_smi_exporter_scrape_success {success}")
    lines.extend(
        [
            "# HELP nvidia_smi_exporter_scrape_duration_seconds nvidia-smi scrape duration in seconds.",
            "# TYPE nvidia_smi_exporter_scrape_duration_seconds gauge",
            f"nvidia_smi_exporter_scrape_duration_seconds {time.time() - start:.6f}",
        ]
    )
    if proc.returncode != 0:
        error = prom_escape((proc.stderr or proc.stdout or "nvidia-smi failed").strip())
        lines.extend(
            [
                "# HELP nvidia_smi_exporter_last_error Last nvidia-smi scrape error.",
                "# TYPE nvidia_smi_exporter_last_error gauge",
                f'nvidia_smi_exporter_last_error{{error="{error}"}} 1',
            ]
        )
        return "\n".join(lines) + "\n"

    rows = csv.reader(proc.stdout.splitlines())
    for key, (metric, metric_type, help_text, _scale) in METRICS.items():
        lines.append(f"# HELP {metric} {help_text}")
        lines.append(f"# TYPE {metric} {metric_type}")

    for row in rows:
        if len(row) != len(FIELDS):
            continue
        values = {field: row[i].strip() for i, field in enumerate(FIELDS)}
        labels = (
            f'gpu="{prom_escape(values["index"])}",'
            f'name="{prom_escape(values["name"])}",'
            f'uuid="{prom_escape(values["uuid"])}"'
        )
        for key, (metric, _metric_type, _help_text, scale) in METRICS.items():
            number = parse_number(values[key])
            if number is None:
                continue
            lines.append(f"{metric}{{{labels}}} {number * scale}")

    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/", "/metrics"):
            self.send_error(404)
            return
        body = collect().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


if __name__ == "__main__":
    listen_addr = os.environ.get("GPU_EXPORTER_LISTEN_ADDR", "127.0.0.1")
    listen_port = int(os.environ.get("GPU_EXPORTER_PORT", "9400"))
    ThreadingHTTPServer((listen_addr, listen_port), Handler).serve_forever()
