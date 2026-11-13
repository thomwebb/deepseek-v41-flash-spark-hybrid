# Worker-loss recovery fix (2026-11-12)

A worker reboot left rank 1 stopped while rank 0's HTTP listener still reported
healthy. Inference stalled and requests accumulated. The old supervisor watched
only `docker wait` on the head; that was insufficient.

A paired restart also exposed a stale RDMA address index: the saved configuration
used GID slot 3, but the head's valid IPv4 RoCE-v2 address was now slot 4. NCCL
reported `ibv_modify_qp` failure with a zero local GID. The worker still used slot 3.
Both NCCL and TensorFold's separate RDMA path need the correct local slot.

Changes:

- Container startup discovers a common live IPv4 RoCE-v2 slot across configured
  HCAs on each host, sets `NCCL_IB_GID_INDEX` and `TF_RDMA_GID_INDEX`, and refuses
  startup if none exists. Original private snapshots remain unchanged.
- Supervisor checks worker container state through bounded SSH every 10 seconds.
  Three consecutive failed checks trigger cleanup and systemd restart of the pair.
- Startup readiness checks the worker as well as the head, avoiding a long wait
  when the follower has already exited.

Validation:

- Real completion returned the expected `323` after service restoration.
- Stopping only the worker triggered the new watchdog and replacement of both
  containers without manual intervention. Ready was observed 54.3 seconds after
  monitoring began following the stop.
- Post-recovery API contracts passed 20/20; clone safety tests passed 7/7,
  including moved-GID discovery and fail-closed rejection of missing live GIDs.
- Qualified engine image and original configuration checksums remain unchanged.

Evidence is in `evidence/recovery-20261112/`.

Limits: this watchdog detects worker container loss/unreachability, not every
possible inference deadlock inside a still-running container. `/health` alone
must not be interpreted as proof that distributed inference is progressing.
The induced test stopped rank 1; it did not reboot the worker again.
