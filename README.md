# DeepSeek-V4.1-Flash on 2x DGX Spark: original-speed hybrid with constrained decoding

A **rank-0 host-side grammar overlay** for the existing
[MiaAI-Lab DeepSeek-V4.1-Flash-1017 TensorFold recipe](https://github.com/MiaAI-Lab/DeepSeek-v4.1-Flash-1017-2x-DGX-Sparks).

It keeps the installed engine, its five Mia patches, the original EXL3 weights, the
GPU math, the tensor-parallel protocol and the ordinary sampler **exactly as they were**,
and adds one capability: **decoder-enforced structured output** (JSON Schema, regex,
choice, EBNF) on rank 0.

No new base model. No new kernels. No measurable quality or throughput change for
ordinary requests in matched testing. The goal was to add structured decoding
without taking the measured slowdown of a wholesale engine replacement.

## Performance (2026-11-04)

Measured on the same two DGX Sparks with the same benchmark, prompts, seeds and
requested concurrency. Each recipe retained its weights and settings. All runs
below were collected after reboot, after one discarded warm-up per recipe.
**Each row is one measured run; no cross-run averages or medians are shown.**
Within each run, code/prose TPS are the harness's median across three requests;
four-request aggregate is total completion tokens divided by concurrent batch time.

| Recipe / individual run | Code tok/s | Prose tok/s | Four-request aggregate tok/s | Functional checks passed | Quality score* |
|---|---:|---:|---:|---:|---:|
| [Mia](https://github.com/MiaAI-Lab/DeepSeek-v4.1-Flash-1017-2x-DGX-Sparks), run 1 | 100.72 | 73.42 | 170.92 | 13/14 | 97.2%; 766/807 |
| Mia, run 2 | 100.62 | 73.30 | 170.85 | 13/14 | 97.2%; 766/807 |
| [Jay](https://github.com/jayleaton/deepseek-v41-tensorfold-spark), run 1 | 90.07 | 64.15 | 120.55 | 14/14 | 96.7%; 763/807 |
| Jay, run 2 | 90.13 | 63.81 | 48.60 | 14/14 | 96.7%; 763/807 |
| **Hybrid, run 1** | **101.77** | **74.16** | **172.73** | **14/14** | **97.2%; 766/807** |
| **Hybrid, run 2** | **101.91** | **74.12** | **173.01** | **14/14** | **97.2%; 766/807** |
| Hybrid, closing repeat | 97.30 | 70.82 | 167.78 | 14/14 | 97.2%; 766/807 |

- **Speed preserved, not a proven speedup over Mia:** the closing hybrid repeat
  shows session drift larger than its opening advantage over Mia.
- **Jay's concurrent throughput was unstable:** both measured results are shown
  rather than combined into a single capacity estimate.
- Mia's sole failed API benchmark check was JSON Schema; ordinary checks passed.
- *Quality uses the latest available 807-item runs, collected separately from
  this TPS comparison. Displayed percentages are category averages. All three
  had zero request errors; Mia and hybrid had identical paired pass/fail results.
- These are ordinary decoding rates, **not constrained-output throughput**.

The hybrid also passed all 12 long-context recall checks through 256K and starts
model serving automatically after reboot: observed ready in **45.76 seconds**
after head boot, with zero supervisor retries.

[Raw comparison results](evidence/fresh-comparison-20261104) ·
[Methodology and historical measurements](docs/MEASUREMENTS.md) ·
[Boot test](evidence/reboot-20261104/reboot.json)

## What actually changed

One patch, 12 files, 581 lines: [patches/0001-host-side-grammar-backport.patch](patches/0001-host-side-grammar-backport.patch).

- A dynamic loader plus `Compiler`/`Constraint` wrapper for the TensorFold grammar C ABI
  (`zig/src/core/lanes/grammar.zig`), with compilation serialised behind a mutex.
- Grammar masks applied to **host logits before sampling**, so no draft token can
  bypass them. Structured requests therefore **disable speculative decoding**.
- Request-scoped constraint lifetime through `engine_api.zig`, `lane_host.zig`,
  `server.zig`, `chat.zig`, `stream.zig`, `families/dsv41/lanes.zig`.
- Fail-closed validation: malformed grammars, unknown reasoning-close tokens, and
  unsupported combinations (forced reasoning-budget closure + structured output,
  forced tool choice + schema) are rejected rather than silently ignored.

Rank 1 runs the **unmodified original image**. Only rank 0 needs the overlay.

## Layout

```
patches/        the backport patch and the no-RUN overlay Dockerfile
docs/           runbook, design rationale, measurement method
runtime.py      shared lifecycle: Docker API, health, guarded runs
grammar_runtime.py   start/stop the rank pair, readiness, memory guards
supervise_grammar.py systemd-owned supervisor (restarts on head exit)
verify_grammar.py    fail-closed acceptance gate for a built image
promote_grammar.py   extended contracts, soak, session probes, recovery drill
install-supervisor.sh installs the head unit and enables user linger, as whoever runs it
grammar_rollback.sh  one-command return to the untouched original deployment
docker_clone.py      clone baseline containers via the local Docker API, no secret argv
api_contract.py      20 API/stream/schema tests
extended_contract.py regex, choice, EBNF, JSON-object, rejection cases
mixed_soak.py        long/short/structured concurrency soak
mixed_decode_probe.py simultaneous ordinary code + constrained arrays
session_probe.py     prefix-cache and multi-turn continuation probes
rollback_drill.py    prove rollback works, then restore the qualified hybrid
evidence/            recorded acceptance, rollback and boundary-test results
```

Flat layout on purpose: it mirrors the directory that is actually deployed, so the
file you read is the file that ran.

## Requirements

- Two DGX Sparks cabled directly (ConnectX-7), passwordless SSH from head to worker.
- The original Mia recipe installed and working, with its `scripts/memguard.sh`.
- Zig 0.17.0 for the native build (CPU-only, 4 GiB cgroup is enough).
- A TensorFold build that provides `libtfgrammar.so`. **This repo does not redistribute
  that binary**; take it from your own TensorFold build or the image you already trust.
- The Mia EXL3 weights at revision `45ee5d0fd106c94a3d77f721e09cbb2f50e5a16b`.
  Weights are **not** included here.

Environment overrides (defaults reproduce the reference deployment):

```
SPARK_HYBRID_ROOT   ~/spark-hybrid-20261031
SPARK_WORKER        10.100.40.1  (no user: ssh logs in as whoever runs the recipe; set user@host to override)
SPARKDASH_QUALITY_URL  required for the SparkDash quality runner; the rollback scripts
                       use it to cancel their own run, and skip that step when unset
```

## Reproduce

1. `git clone` the Mia recipe at commit `31eea46`; confirm the five deployed patches.
2. Apply `patches/0001-host-side-grammar-backport.patch` to upstream TensorFold
   `629c524a` plus those five patches; the result is commit `487088ae`.
   Verified: applying the patch to `11737b98` (the exact five-patch baseline)
   reproduces tree `af382d6e`, byte-for-byte identical to the deployed commit.
3. `build_native.sh` (CPU-only container, real cgroup limit), then
   `assemble_grammar.py` to produce the overlay image. `grammar_library_probe.py`
   proves the ABI against the real tokenizer before anything is served.
4. `grammar_trial.py --quality` benchmarks and quality-tests the candidate, and
   **restores the original before promotion**.
5. `verify_grammar.py` refuses to bless an image unless every contract, speed ratio,
   coding check and paired quality item passes.
6. `install-supervisor.sh` puts `dsv41-hybrid.service` in `~/.config/systemd/user/`,
   enables **user linger**, and enables the unit. Linger is what makes it start after a
   reboot with nobody logged in; `systemctl --user enable` alone does not do that.
   `install-supervisor.sh --check` reports all three facts at any time.
7. `promote_grammar.py` installs `dsv41-hybrid.service`, refuses to run while linger is
   off, disables the original unit, and re-checks speed, mixed soak and session behaviour
   across an induced pair restart.

Full detail, including the exact commands and the recovery semantics:
[docs/HYBRID-RUNBOOK.md](docs/HYBRID-RUNBOOK.md).

## Limits - read these

- **Not a smarter model.** Same weights, same quality, by measurement.
- **Structured output is non-speculative**, so it is slower than ordinary decoding.
- **Token caps still bite.** Two adversarial 64-item array probes spent a 512-token
  budget on legal whitespace and returned `finish_reason: length`; both concurrent
  coding tasks in the same test passed. Compact guidance plus 4,096 tokens completed
  one array and still capped the other. A prompt that described the wanted values
  completed both within 512 tokens. Grammar masks constrain *valid* tokens, they do
  not guarantee *completion* - check the finish reason before parsing.
- **No disk sessions, no KV sharding/compaction/pruning.** Upstream RAM prefix caching
  is retained: 5.20 s cold vs 0.92 s on a 10,240-token repeat, cold again after a
  process restart. That is caching, not persistence.
- **Real OS reboot tested on both hosts.** Automatic startup reached ready 45.76 s
  after head boot, with zero retries; post-boot contracts passed 20/20. Worker booted
  ~43 s earlier. Abrupt power loss and the reverse boot ordering remain untested.
- **1M context not exercised.** 256K was.

## Rollback

```
ssh spark 'sh ~/spark-hybrid-20261031/grammar_rollback.sh'
```

Exercised after promotion: original image and unit restored and verified in 40.31 s,
qualified hybrid back in 37.28 s. Original unit file and local settings are
checksum-verified unchanged, and `dsv41-hybrid.service` declares
`Conflicts=dsv41.service` so the two supervisors cannot start together.

## Licence and provenance

Our scripts and the backport patch are Apache-2.0. The engine is a fork of
[TensorFold](https://github.com/ashhart/TensorFold) 1.0.2 (Apache-2.0) by BertholomusAI;
upstream `LICENSE`, `NOTICE` and third-party notices must travel with any redistribution.
The base recipe and weights belong to MiaAI-Lab. See [NOTICE](NOTICE).

Nothing here grants rights to the model weights or to any prebuilt image.
