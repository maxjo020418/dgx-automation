# DGX LLM Stack Automation

This directory is the control repo for the DGX Spark LLM stack. The intended
workflow is:

```bash
make validate
make render
make apply
make smoke
```

Edit these files:

```text
stack.yml   # host, domain, service, path, image, and global GPU settings
models.yml  # vLLM model fleet and per-model serving settings
```

Do not manually edit these files:

```text
generated/*
/opt/dgx-llm-stack/* on the server, except emergency debugging
```

`make apply` renders local config, copies it to `/opt/dgx-llm-stack` on
`ymin@spark.cg-rookies.net`, archives the previous release, and runs Docker
Compose detached.

## Current Endpoints

These names resolve through the DGX dnsmasq split-DNS setup:

```text
spark.cg-rookies.net          -> OpenWebUI
chat.spark.cg-rookies.net     -> OpenWebUI
api.spark.cg-rookies.net      -> LiteLLM
grafana.spark.cg-rookies.net  -> Grafana
search.spark.cg-rookies.net   -> SearXNG
```

LiteLLM is currently configured with no API key requirement:

```yaml
services:
  litellm:
    auth_enabled: false
```

Access control is therefore the WireGuard/UFW boundary. Raw vLLM is internal to
Docker and is not exposed on the host.

## Common Commands

```bash
make bootstrap              # install local Ansible/Jinja/PyYAML into .venv
make validate               # validate stack.yml and models.yml
make render                 # regenerate generated/*
make stage                  # copy files and run remote compose config only
make apply                  # deploy; stops legacy containers if configured
make apply STOP_LEGACY=0    # deploy without trying to stop legacy containers
make smoke                  # endpoint and chat-completion checks
make ufw-apply              # ensure WireGuard-scoped UFW rules
make rollback RELEASE=...   # restore a release from /opt/dgx-llm-stack/releases
```

## `stack.yml`

`stack.yml` controls global deployment behavior.

### Project And Deploy Path

```yaml
project_name: dgxllm
deploy_dir: /opt/dgx-llm-stack
```

`project_name` is the Docker Compose project name. `deploy_dir` is where Ansible
copies rendered files on the DGX host.

### Domain

```yaml
domain:
  base: spark.cg-rookies.net
  openwebui: chat.spark.cg-rookies.net
  litellm: api.spark.cg-rookies.net
  grafana: grafana.spark.cg-rookies.net
  searxng: search.spark.cg-rookies.net
```

These values become Traefik `Host(...)` rules. dnsmasq currently maps
`*.spark.cg-rookies.net` to `192.168.111.1`.

### Network

```yaml
network:
  name: dgx-llm-net
  bind_ip: 192.168.111.1
```

`bind_ip` is the WireGuard-side address used for published service ports. Do not
set this to `0.0.0.0` unless you intentionally want services exposed beyond the
VPN boundary.

### Global GPU Policy

```yaml
gpu:
  default_headroom: 0.08
  max_fraction_per_gpu: 0.92
  allowed_device_ids: ["0"]
```

`allowed_device_ids` is the GPU list the validator accepts. DGX Spark currently
uses GPU `0`.

`max_fraction_per_gpu` is the hard validation cap for the sum of enabled model
`gpu.memory_fraction` values on a GPU.

`default_headroom` is used by `scripts/rebalance.py` for weighted rebalance. It
is not directly added by the validator; the validator uses `max_fraction_per_gpu`
as the limit.

### Services

Each service has an `enabled` flag and, when it is host-accessible, an
`expose_host` and `host_port`.

Example:

```yaml
services:
  grafana:
    enabled: true
    internal_port: 3000
    expose_host: true
    host_port: 3300
```

When `expose_host: true`, Compose publishes the service on:

```text
192.168.111.1:<host_port>
```

Prometheus/exporters/raw vLLM should remain internal. User-facing access should
go through Traefik or the WireGuard-bound host ports.

## `models.yml`

`models.yml` is the model fleet. Each enabled entry renders:

```text
one vLLM container
one or more LiteLLM model aliases
one Prometheus scrape target
```

Minimal shape:

```yaml
models:
  - id: gemma-4-26b
    enabled: true
    kind: chat
    backend: vllm
    image: vllm-node
    pull_policy: never
    hf_model: nvidia/Gemma-4-26B-A4B-NVFP4
    served_names:
      - gemma4

    gpu:
      device_ids: ["0"]
      memory_fraction: 0.71

    vllm:
      max_model_len: 262144
      max_num_seqs: 32
      max_num_batched_tokens: 32768
      kv_cache_dtype: fp8
      quantization: modelopt
      trust_remote_code: true
      chat_template: chat-templates/tool_chat_template_gemma4.jinja
      extra_args: []

    routing:
      internal_port: 8000
      expose_host: false
      expose_publicly: false

    litellm:
      mode: chat
```

### Identity

```yaml
id: gemma-4-26b
served_names:
  - gemma4
  - gemma-4-26b
```

`id` becomes the container suffix: `vllm-gemma-4-26b`.

`served_names` are the names clients use through LiteLLM/OpenAI-compatible APIs.
They must be globally unique.

### Image

```yaml
image: vllm-node
pull_policy: never
```

`vllm-node` is the local DGX Spark vLLM image built from
`/home/ymin/spark-vllm-docker`. `pull_policy: never` prevents Compose from trying
to pull it from a registry.

### vLLM Arguments

Fields under `vllm:` render into the `vllm serve ...` command. Common fields:

```yaml
max_model_len: 262144
max_num_seqs: 32
max_num_batched_tokens: 32768
kv_cache_dtype: fp8
quantization: modelopt
trust_remote_code: true
enable_auto_tool_choice: true
enable_prefix_caching: true
tool_call_parser: gemma4
reasoning_parser: gemma4
chat_template: chat-templates/tool_chat_template_gemma4.jinja
extra_args: []
```

Use `extra_args` only for flags the renderer does not expose yet.

Example:

```yaml
extra_args:
  - "--generation-config"
  - "vllm"
```

### Routing

```yaml
routing:
  internal_port: 8000
  host_port: 8101
  expose_host: false
  expose_publicly: false
```

Keep raw vLLM `expose_host: false` unless you have a temporary debugging reason.
LiteLLM should be the model API boundary.

### LiteLLM Options

```yaml
litellm:
  mode: chat
  input_cost_per_token: 0
  output_cost_per_token: 0
  extra_body:
    chat_template_kwargs:
      enable_thinking: true
```

`extra_body` is sent by LiteLLM to vLLM. For Gemma thinking mode, the important
shape is:

```json
{
  "chat_template_kwargs": {
    "enable_thinking": true
  }
}
```

## Memory Allocation

### What `memory_fraction` Means

For each vLLM container:

```yaml
gpu:
  device_ids: ["0"]
  memory_fraction: 0.71
```

`memory_fraction` renders to:

```bash
--gpu-memory-utilization 0.71
```

This is a startup-time vLLM setting. It controls the fraction of GPU memory vLLM
is allowed to use for the model executor and KV cache. It is not a live-tunable
setting; changing it requires recreating/restarting that vLLM container.

### Validator Budget Rule

The validator sums `memory_fraction` for all enabled models assigned to each GPU:

```text
sum(enabled models on GPU 0) <= stack.gpu.max_fraction_per_gpu
```

Current global cap:

```yaml
max_fraction_per_gpu: 0.92
```

Current model:

```yaml
gemma-4-26b: 0.71
```

Current budget:

```text
GPU 0 = 0.71 / 0.92
remaining validator budget = 0.21
```

Example valid two-model allocation:

```yaml
gemma-4-26b:
  memory_fraction: 0.71

embedding-model:
  memory_fraction: 0.08
```

Budget:

```text
0.71 + 0.08 = 0.79 <= 0.92
```

Example invalid allocation:

```text
0.71 + 0.25 = 0.96 > 0.92
```

`make validate` will fail before rendering or deployment.

### Choosing A Fraction

Use a conservative process:

1. Start with one large model at a known-good fraction.
2. Add smaller services with small fractions.
3. Run `make validate`.
4. Run `make apply`.
5. Watch startup logs and Prometheus/Grafana memory metrics.

For large context lengths, leave more headroom. These settings increase memory
pressure, especially KV cache pressure:

```yaml
max_model_len
max_num_seqs
max_num_batched_tokens
```

Reducing any of those may make a lower `memory_fraction` viable. Increasing them
may require a higher `memory_fraction` or fewer colocated models.

### `kv_cache_memory_bytes`

The validator intentionally rejects this combination by default:

```yaml
gpu:
  memory_fraction: 0.71
vllm:
  kv_cache_memory_bytes: ...
```

Reason: both settings control memory sizing, and using both is easy to
misunderstand. If you intentionally want explicit KV cache bytes, set:

```yaml
vllm:
  kv_cache_memory_bytes: 123456789
  allow_fraction_with_kv_cache_memory_bytes: true
```

Prefer `memory_fraction` unless there is a concrete reason to pin KV cache bytes.

### Weighted Rebalance

Manual fractions are the default. Optional weighted rebalance is available for
models with:

```yaml
sizing:
  mode: weighted
  weight: 4
  min_fraction: 0.18
  max_fraction: 0.45
```

Then:

```bash
make rebalance
```

The rebalance script computes per-GPU available budget as:

```text
stack.gpu.max_fraction_per_gpu - stack.gpu.default_headroom
```

With current settings:

```text
0.92 - 0.08 = 0.84
```

It then divides that available budget by model weights on the same GPU, clamped
by each model's `min_fraction` and `max_fraction`.

Rebalance updates `models.yml`; it does not deploy by itself.

## Add A Model

1. Add a new entry to `models.yml`.
2. Use a unique `id`.
3. Use unique `served_names`.
4. Assign `gpu.device_ids`.
5. Pick a conservative `gpu.memory_fraction`.
6. Keep raw vLLM internal:

```yaml
routing:
  internal_port: 8000
  expose_host: false
  expose_publicly: false
```

Then run:

```bash
make validate
make render
make apply
make smoke
```

## Remove A Model

Set:

```yaml
enabled: false
```

Then:

```bash
make apply
```

Compose runs with `--remove-orphans`, so removed generated services disappear.

## Rollback

Each apply archives the previous runtime files under:

```text
/opt/dgx-llm-stack/releases/
```

Rollback:

```bash
make rollback RELEASE=<release-directory>
```

Example:

```bash
make rollback RELEASE=20260515T163517
```
