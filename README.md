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

`make apply` renders local config, copies only the vLLM/LiteLLM runtime to
`/opt/dgx-llm-stack` on the inventory target, archives the previous release,
starts vLLM through `/home/ymin/spark-vllm-docker/launch-cluster.sh`, and runs
the LiteLLM Compose service detached.

Support services are split into `/opt/dgx-services` and are intended to be
managed directly on the DGX host after migration. `make migrate-services` is a
seed/bootstrap helper: it creates missing files from `files/dgx-services/*`, but
does not overwrite existing `/opt/dgx-services/compose.yml`,
`/opt/dgx-services/traefik-dynamic.yml`, or service config files.

## Current Endpoints

Public routing uses the base hostname plus paths:

```text
pika.ihopper.co.kr/                    -> OpenWebUI
pika.ihopper.co.kr/llm/v1              -> LiteLLM OpenAI-compatible API
pika.ihopper.co.kr/grafana/            -> Grafana
pika.ihopper.co.kr/searxng/            -> SearXNG
pika.ihopper.co.kr/traefik/dashboard/  -> Traefik dashboard
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
make render                 # regenerate model-stack files in generated/*
make services-compose-config # validate /opt/dgx-services/compose.yml on the host
make migrate-services       # seed missing /opt/dgx-services files and start that stack
make stage                  # copy files and run remote compose config only
make apply                  # deploy only vLLM + LiteLLM
make apply PULL_IMAGES=0    # deploy without checking/pulling registry images
make migrate-services SERVICES_PULL_IMAGES=0
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
copies the vLLM/LiteLLM files on the DGX host. The support-service compose
project is `dgx-services` and lives at `/opt/dgx-services`.

### Domain

```yaml
domain:
  base: pika.ihopper.co.kr
  openwebui: chat.pika.ihopper.co.kr
  litellm: api.pika.ihopper.co.kr
  grafana: grafana.pika.ihopper.co.kr
  searxng: search.pika.ihopper.co.kr
  traefik: traefik.pika.ihopper.co.kr
```

The public Traefik routers currently use `domain.base` with path prefixes.
dnsmasq only needs to map `pika.ihopper.co.kr` to `10.90.0.103` unless
subdomain routers are restored later.

### Network

```yaml
network:
  name: dgx-llm-net
  bind_ip: 10.90.0.103
```

`bind_ip` is the WireGuard-side address used for published service ports. Do not
set this to `0.0.0.0` unless you intentionally want services exposed beyond the
VPN boundary.

Both compose projects join the external Docker network named by `network.name`.
The playbooks create it when missing.

### Global GPU Policy

```yaml
gpu:
  default_headroom: 0.08
  max_fraction_per_gpu: 0.92
  validate_memory_fraction_budget: false
  allowed_device_ids: ["0"]
```

`allowed_device_ids` is the GPU list the validator accepts. DGX Spark currently
uses GPU `0`.

`max_fraction_per_gpu` is the hard validation cap for the sum of enabled model
`gpu.memory_fraction` values on a GPU.

`default_headroom` is used by `scripts/rebalance.py` for weighted rebalance. It
is not directly added by the validator; the validator uses `max_fraction_per_gpu`
as the limit.

### Deployment

```yaml
deployment:
  vllm:
    launcher_dir: /home/ymin/spark-vllm-docker
    launcher_home: /home/ymin
    earlyoom: true
    staggered_start: true
    startup_timeout_seconds: 1800
    startup_poll_seconds: 5
    startup_settle_seconds: 10
```

`make apply` starts one `vllm-*` container at a time with
`launch-cluster.sh --solo --earlyoom -d`, waits for
`http://127.0.0.1:8000/health` from inside the container, then starts the next
one. The probe uses the model's configured `routing.internal_port`.

This healthcheck is the readiness signal that makes staggering meaningful. A
plain container start only means the process exists; it does not mean vLLM has
finished loading the model and sizing GPU memory.

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
10.90.0.103:<host_port>
```

Prometheus/exporters/raw vLLM should remain internal. User-facing access should
go through Traefik or the WireGuard-bound host ports.

Traefik routes are file-based after migration. Edit this file on the DGX host to
change or add reverse proxy entries:

```text
/opt/dgx-services/traefik-dynamic.yml
```

Prometheus is managed by the services stack, but it mounts the Ansible-generated
vLLM target file from:

```text
/opt/dgx-llm-stack/prometheus-vllm-targets.yml
```

## `models.yml`

`models.yml` is the model fleet. Each enabled entry renders:

```text
one vLLM launch script
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
      recipe: recipes/gemma4-26b-a4b-nvfp4.yaml
      max_model_len: 262144
      max_num_seqs: 32
      max_num_batched_tokens: 32768
      tensor_parallel_size: 1
      load_format: instanttensor
      kv_cache_dtype: fp8
      trust_remote_code: true
      extra_args: []

    routing:
      internal_port: 8000
      host_port: 8101
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
`/home/ymin/spark-vllm-docker`. vLLM containers are launched from that repo with
the generated launch script. `pull_policy` is retained as model metadata, but
vLLM is no longer pulled or started by Compose.

### vLLM Arguments

Fields under `vllm:` render into the generated `vllm serve ...` launch script.
`recipe` records the Spark recipe this model is aligned with; the active Gemma
deployment refers to `recipes/gemma4-26b-a4b-nvfp4.yaml`. Common generation
fields:

```yaml
max_model_len: 262144
max_num_seqs: 32
max_num_batched_tokens: 32768
tensor_parallel_size: 1
pipeline_parallel_size: 1
kv_cache_dtype: fp8
load_format: instanttensor
trust_remote_code: true
enable_auto_tool_choice: true
enable_prefix_caching: true
enable_chunked_prefill: true
async_scheduling: true
generation_config: vllm
speculative_config:
  method: mtp
  model: google/gemma-4-26B-A4B-it-assistant
  num_speculative_tokens: 4
  moe_backend: triton
tool_call_parser: gemma4
reasoning_parser: gemma4
mm_processor_kwargs:
  max_soft_tokens: 1120
engine_args:
  structured_outputs_config:
    backend: xgrammar
    disable_any_whitespace: true
extra_args: []
```

Use `engine_args` for vLLM CLI flags that are not first-class fields in this
repo yet. Keys may use underscores or dashes and render as long-form CLI flags:

```yaml
engine_args:
  structured_outputs_config:
    backend: xgrammar
    disable_any_whitespace: true
  logits_processor_pattern: ".*"
  limit_mm_per_prompt:
    image: 1
```

This renders as:

```text
--structured-outputs-config '{"backend":"xgrammar","disable_any_whitespace":true}'
--logits-processor-pattern .*
--limit-mm-per-prompt '{"image":1}'
```

Boolean `true` renders a flag with no value. Boolean `false` and `null` are
omitted. Mappings and lists are rendered as compact JSON. Use `extra_args` only
when you need exact raw CLI tokens.

For vLLM builds that expose the older/newer guided-decoding flag directly, the
same field can pass it as a boolean flag:

```yaml
engine_args:
  guided_decoding_disable_any_whitespace: true
```

For embedding models, prefer the current vLLM pooling runner fields when your
image supports them:

```yaml
vllm:
  runner: pooling
  convert: embed
  pooler_config:
    pooling_type: MEAN
```

Older vLLM images may still require the deprecated task selector instead:

```yaml
vllm:
  task: embed
```

Per-model environment variables are passed to that model's launcher container:

```yaml
env:
  VLLM_MARLIN_USE_ATOMIC_ADD: "1"
```

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
`host_port` is still required: when `expose_host: false`, Ansible passes a
loopback-only `127.0.0.1:<host_port>:<internal_port>` mapping to
`launch-cluster.sh` so the container can join `network.name` instead of using
Docker host networking.
LiteLLM should be the model API boundary.

### LiteLLM Options

```yaml
litellm:
  mode: chat
  params:
    input_type: query
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

`params` is rendered directly under LiteLLM `litellm_params`. This is useful for
embedding-specific provider options such as `input_type`.

Embedding model example:

```yaml
models:
  - id: bge-m3
    enabled: false
    kind: embedding
    backend: vllm
    image: vllm-node
    pull_policy: never
    hf_model: BAAI/bge-m3
    served_names:
      - bge-m3

    gpu:
      device_ids: ["0"]
      memory_fraction: 0.08

    vllm:
      runner: pooling
      convert: embed
      max_model_len: 8192
      trust_remote_code: true

    routing:
      internal_port: 8000
      host_port: 8106
      expose_host: false
      expose_publicly: false

    litellm:
      mode: embedding
      input_cost_per_token: 0
```

Clients still use OpenAI-compatible APIs through LiteLLM. Embeddings go to
`/v1/embeddings` with `model` set to one of the configured `served_names`.

## Memory Allocation

### What `memory_fraction` Means

For each vLLM container:

```yaml
gpu:
  device_ids: ["0"]
  memory_fraction: 0.71
```

`memory_fraction` renders into the generated vLLM launch script as:

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
validate_memory_fraction_budget: false
```

Set `validate_memory_fraction_budget: true` to make `make validate` reject
over-budget totals. Set it to `false` to keep the per-model sanity checks but
allow experiments where the summed fractions exceed `max_fraction_per_gpu`.
This does not change vLLM runtime behavior; it only controls validation.

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
  host_port: 8101
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

`make apply` removes and recreates the named vLLM launcher container for enabled
models. The LiteLLM Compose apply still runs with `--remove-orphans`; it does
not touch `/opt/dgx-services`.

## Rollback

Each apply archives the previous runtime files under:

```text
/opt/dgx-llm-stack/releases/
```

Release archives are pruned automatically after each apply. The retention count
is controlled by:

```yaml
deployment:
  release_retention:
    keep: 5
```

Rollback:

```bash
make rollback RELEASE=<release-directory>
```

Example:

```bash
make rollback RELEASE=20260515T163517
```
