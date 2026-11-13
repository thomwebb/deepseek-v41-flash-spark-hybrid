# DeepSeek V4.1 Flash: original Mia numerical engine plus structured output

Status: **deployed and serving under systemd**, with the original deployment preserved and the documented rollback exercised end-to-end.

Public source and evidence: https://github.com/thomwebb/deepseek-v41-flash-spark-hybrid

## What this actually combines

This is not the whole Jay deployment with a few environment changes. That trial was slower. It is a small host-side backport onto the exact deployed Mia engine:

- Upstream engine: `629c524a0ea57512a5342abb498d1b7c25353343`.
- The original five installed Mia patches, recorded as baseline commit `11737b98`.
- Structured-output backport: `487088aeda6f19d9e6328ff273b9c1edbfdbdf83`.
- The xgrammar runtime from the tested Jay image, using TensorFold `20ad77878b14203ff30727054c718c70d36ff6c5`'s C ABI.
- Original Mia EAQ 2.9-bpw checkpoint, revision `45ee5d0fd106c94a3d77f721e09cbb2f50e5a16b`.

Rank 0 runs `tensorfold-dsv41:hybrid-grammar-487088aeda6f`. Rank 1 deliberately retains the original runtime image. The TP protocol, model weights, GPU kernels, ordinary sampler, and ordinary speculative decoding are unchanged. The new logic is API parsing and host-side grammar masking on rank 0. The only changed runtime environment key is `TF_HYBRID_GRAMMAR`.

The endpoint remains port **8888**, with model ID **DeepSeek-V4.1-Flash-1017**.

## Matched performance screen

Three repetitions for the sequential measurements, using the same benchmark manifest and settings:

| Measurement | Original | Hybrid | Difference |
|---|---:|---:|---:|
| Code decode, approximate tok/s | 91.064 | 90.912 | -0.17% |
| Prose decode, approximate tok/s | 66.300 | 66.145 | -0.23% |
| Four-request aggregate tok/s | 154.784 | 155.347 | +0.36% |
| Cold approximately 8K prefill TTFT | 3.817 s | 3.822 s | +0.15% |
| Functional benchmark cases | 13/14 | 14/14 | JSON schema now supported |

After deployment and automatic recovery, the approximate rates were 90.831 code tok/s, 65.904 prose tok/s, and 154.453 aggregate tok/s: respectively 0.26%, 0.60%, and 0.21% below the matched original baseline.

The warmed hybrid prefill repeat took 1.134 s, but restored **6,144 cached prompt tokens**. That is a cache hit, not evidence of a faster prefill kernel. The code/prose throughput rows had zero cached prompt tokens. Ordinary throughput is effectively unchanged within this experiment's noise.

The earlier whole Jay image was about 12% slower on warmed prose and 22% slower on concurrent throughput. The later new-engine/original-Mia-weights screen was also slower: approximately 22% on prose and 29% on concurrent throughput. Blocking versus spin host scheduling did not fix it. A first auto profile also refused its memory-admission estimate; its 4 GiB hard floor was not lowered to force a pass.

## Quality and protocol evidence

Real persisted SparkDash quality run:

- Baseline: `273d9930-c819-4c52-afd4-0e2848cd26d5`.
- Hybrid: `d89c3476-cbd7-4006-9bcb-cc988ace3d0a`.
- 807 items, same suite/scoring versions, concurrency, categories, and long-context settings.
- Both displayed **97.2%** category-average overall.
- Both scored **766/807** raw items correctly.
- Zero request errors in either run.
- Identical pass/fail outcomes on every paired item: zero losses and zero gains in every category.
- All 12 long-context recall items through **256K** passed, covering 192 recall keys.

This is strong evidence against a regression on this suite, not proof of universal model equivalence or a 1M-context soak.

Additional checks:

- 43 host-side lane tests passed, including grammar lifetime, rejection, reasoning transition, and isolation tests.
- Six Docker-management safety tests passed without using a daemon or real credentials.
- The original runtime successfully loaded the actual grammar library and compiled all six schema families against the real checkpoint tokenizer; malformed schema rejected.
- 20/20 GPU API contracts passed: JSON and SSE, nested/const/enum/array/bounds/escaping/pattern constraints, concurrent schema isolation, thinking-on requests, malformed-schema rejection before streaming, and native PNG image input.
- 5/5 generated-code checks passed in no-network, no-GPU, read-only, resource-limited sandboxes.
- 8/8 deployment-time extended contracts passed: guided regex, choice, EBNF, JSON object mode, and explicit rejection of invalid or unsupported combinations.

Deployment validation also completed:

- 16/16 mixed-traffic checks across four rounds of approximately 96K and 32K ordinary prompts, a short structured stream, and a short ordinary stream.
- 3/3 prefix/continuation probes before the intentional head exit and 3/3 afterward.
- Automatic replacement of both ranks after the intentional head exit, healthy again in **43.47 s**.
- A separate simultaneous ordinary-code/constrained-array control passed **4/4**, including hidden-property execution of both generated programs.
- The documented rollback actually restored the original image and original active/enabled unit in **40.31 s**, passed an original API smoke check, and then restored the qualified hybrid in **37.28 s**.

The prefix probe restored 10,240 RAM-cached tokens: approximately 5.20 s cold versus 0.92 s repeated. After process restart its first request was cold again (5.41 s, zero cached tokens), followed by a 0.94 s RAM-cache hit. This is preserved RAM caching, not newly added disk persistence.

Deployment-specific evidence is written under `results/grammar-promotion-*/`; `results/grammar-deployment.json` is the acceptance record, and `results/grammar-rollback-drill.json` records the later rollback-and-return drill. Do not infer success merely from a container name or a running detached prototype.

## Scope and deliberate limitations

- This is the same base model and the same quantized checkpoint, not a smarter or newer model.
- Structured requests use **non-speculative decoding** so that no draft can bypass a grammar mask. Their throughput is not claimed to match the ordinary code/prose numbers above.
- **Token limits still matter.** In an extra adversarial 64-item array probe, both JSON streams exhausted a 512-token cap on legal whitespace and returned `finish_reason: length`; both simultaneous coding tasks passed. With compact guidance and 4,096 tokens, one array completed and one still hit the cap. A cooperative prompt describing the requested values completed both arrays within 512 tokens, with all four simultaneous checks passing. The original failed/truncated records are retained as `results/grammar-mixed-decode*.json`.
- Grammar masks enforce allowed continuations; they do not guarantee completion under every cap. Check `finish_reason` before treating a response as complete JSON, describe the desired content in the prompt, and provide adequate output-token headroom. Upstream finite-budget grammar and forced-grammar-draft optimizations have not been backported.
- Existing ordinary forced tool selection remains available. Combining forced tool selection with `response_format` is rejected, rather than silently under-enforced.
- Structured requests with a forced reasoning budget/loop closure are explicitly rejected. Ordinary thinking-on structured output is supported and tested.
- The existing RAM prefix cache is retained. Repeated-prefix and standard multi-turn continuation probes are recorded before and after the controlled restart.
- **Persistent disk sessions, KV sharding, KV compaction, and pruning have not been backported or enabled.** The large newer-Mia spill/sharding patch series is a separate engine update. Its environment flags are not a feature switch for this old engine.
- Original image handling is retained; no claim is made that the newer native JPEG/WebP decoder stack was imported.
- The KV pool, context settings, prefill policy, and routing policy remain those of the original deployment. The full quality suite tested individual contexts through 256K, not the advertised maximum 1M.

## Supervision and recovery

After successful promotion:

- `dsv41-hybrid.service` is the sole active/enabled model supervisor on the head.
- Original `dsv41.service` is stopped/disabled, but its unit file and local settings are unchanged and checksum-verified. The new unit also declares `Conflicts=dsv41.service` with ordering, so systemd will not start both model supervisors together.
- Both Docker ranks have restart policy `no`; systemd owns pair restart so two independent supervisors cannot race.
- The worker has no competing model unit. The head has user lingering enabled by `install-supervisor.sh`, and starts the worker over the same passwordless SSH path as the original deployment.
- The original worker NFS volume and rank/Engram mounts are retained. Docker's volume configuration persists across reboot.
- SSH/network startup failures cause bounded failed starts followed by systemd retry.
- Memory guards watch both ranks: soft floor 3.5 GiB held for 2 s, hard floor 2.5 GiB. These use OS `MemAvailable`, because GPU and CPU share Spark memory.
- Promotion intentionally stops the head container and requires systemd to replace **both** rank containers and recover health. The result is recorded in the deployment report.

A real two-host OS reboot was subsequently tested on 2026-11-04. Both boot IDs changed; the hybrid started without manual intervention and was ready 45.76 seconds after head boot, with zero supervisor retries. That test exercised a head where linger was **already** enabled; it did not prove that a fresh install would come back on its own, which is why linger is now set by an installer and gated before promotion. The worker booted approximately 43 seconds earlier, so this does not prove every boot ordering or an abrupt power-loss scenario. Post-boot: 20/20 API contracts, 3/3 saved-prefix/continuation checks, and 14/14 benchmark checks passed. Evidence: `evidence/reboot-20261104/reboot.json` in the public repository.

### What happens at boot

Boot wiring inspected and automatic startup observed in the real reboot above:

- Head user lingering is enabled for whoever runs the recipe (`loginctl show-user "$(id -un)" --property=Linger` -> `yes`), so the user manager starts without a login. `install-supervisor.sh` sets it; `promote_grammar.py` refuses to promote while it is off.
- `dsv41-hybrid.service` is enabled and linked from `default.target.wants`, so it is pulled in at boot.
- Neither fact is implied by installing the unit. `systemctl --user enable` only creates the `default.target.wants` link; it does not enable linger. A unit that is enabled but not lingering is silently dead after every reboot.
- `docker.service` is enabled on both hosts; `ssh.socket` is enabled on both (socket-activated SSH is normal on Ubuntu, `ssh.service` shows `disabled`).
- The head exports its Hugging Face cache read-only to `10.100.40.1`, and `nfs-server` is enabled and active. With the default user-agnostic worker the export is `/home/$(id -un)/.cache/huggingface`.
- The worker has no model unit of its own; it is started remotely by the head supervisor over the same passwordless SSH path.
- Neither rank container has a Docker restart policy, so there is exactly one supervisor after boot.

Ordering caveat: `After=network-online.target` in a **user** unit does not pull that target in, so it is effectively a no-op. The supervisor may start before Docker or the fabric link is ready. That is covered by `Restart=always`, `RestartSec=10` and `StartLimitIntervalSec=0`: a failed start exits non-zero and systemd retries indefinitely.

### Cold start is not free

The service loads model data into unified memory automatically. The installed deployment retains per-rank disk caches: on the real reboot, each rank reported reading 95.1 GiB of expert entries in approximately 9-10 seconds. It did not reread the full safetensors checkpoint through NFS as previously estimated.

Measured on the current pair:

- Head local NVMe, direct I/O: **3.0 GB/s** -> ~66 s for 197 GB.
- Worker over NFS from the head cache: **1.2 GB/s** -> ~164 s for the same 197 GB (that measurement was against a warm head page cache, so treat it as a lower bound).

The worker's `/hf` is a Docker `local` volume backed by NFS4 (`addr=10.100.40.2`, `ro`, `hard`), mounted by Docker at container start. Because it is a `hard` mount, an unavailable export blocks I/O rather than failing fast.

The earlier **3-4 minute** estimate extrapolated full-checkpoint reads and was not representative of this installed cached deployment. Observed head boot-to-ready was **45.76 seconds**, including approximately 36 seconds from supervisor start. Fresh/cacheless builds remain untested. `ready()` retains a fixed 600-second deadline; an unusually slow start can still cause a retry.

Disk headroom is also tight on the head: 324 GB free on a 3.7 TB root filesystem (91% used) with a 711 GB HF cache.

Useful commands on the head:

```sh
./install-supervisor.sh --check
systemctl --user status dsv41-hybrid.service
journalctl --user -u dsv41-hybrid.service -f
curl -fsS http://127.0.0.1:8888/health
python3 ~/spark-hybrid-20261031/verify_grammar.py
```

## One-command rollback

From the workstation:

```sh
ssh spark 'sh ~/spark-hybrid-20261031/grammar_rollback.sh'
```

This was exercised successfully after promotion. It disables/stops the hybrid supervisor, cancels only its own still-running dashboard job if needed, stops only its rank containers and guards, enables/starts the untouched original unit, and waits for original API health. Merely killing a container is not a rollback when a supervisor is configured to restart it.

## Reproducibility and private data

The durable directory on the head is `~/spark-hybrid-20261031`.

- `grammar-source.bundle`: a complete Git history containing branch `spark-hybrid-grammar` at the backport commit.
- `grammar-build/context/backport.patch`: code-only diff from the deployed five-patch baseline.
- `grammar-build/context/build-provenance.json`: source, base image, checkpoint, executable, and grammar-library hashes.
- `grammar-build/image-manifest.json`: this host's content-addressed image identity.
- `grammar-qualification/results/qualified-image-manifest.json`: the frozen identity actually GPU-qualified.
- `grammar-qualification/results/`: full matched benchmark, API, coding, and persisted SparkDash results.
- `results/grammar-deployment.json`: final supervision, mixed-traffic, recovery, and post-recovery gates.
- `rollback/`: original configuration checksums and private Docker snapshots.

For example, to inspect the source without modifying a deployment:

```sh
git clone --branch spark-hybrid-grammar \
  ~/spark-hybrid-20261031/grammar-source.bundle \
  ~/spark-hybrid-grammar-source
```

The image is a roughly 26 MB build-context overlay on the original runtime; its Dockerfile has no `RUN` steps. Compilation was performed separately in an enforced 4 GiB CPU-only cgroup during maintenance. An attempted 1 GiB live compiler was correctly OOM-contained without killing the model. Do not rerun old lab controllers against an active promoted service: they were written for controlled maintenance phases, not as a general-purpose upgrade manager.

Docker snapshots can contain inherited credentials. They are private mode 0600, are not in the Git bundle, and must not be published. The source bundle and image provenance contain no inherited runtime credentials. No new public listening port or authentication policy was introduced. As before, keep the endpoint on a trusted network; grammar compilation runs inside the serving process.
