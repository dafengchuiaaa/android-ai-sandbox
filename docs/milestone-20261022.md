# 2026-10-22 milestone: bounded agent operations and measured resource baseline

This is a development milestone, not a memory-reduction release. Scoped input and repository-refresh operations succeeded; the process-lifecycle and resource observations below apply to their bound development versions, not every machine or arbitrary app/task.

## Functional observations

An external Gemini agent read each committed formal response and chose the next exact action: launch, fresh observe, Search click, Aurora set-text, independent same-field text/focus observe, finish. The worker and host agreed on completion. The refresh run separately verified official repository identity, UI completion and canonical same-chain network evidence. Normal Stop, owned-process exit and exact restoration of the 112-file development baseline were checked for the corresponding runs. No human typing/signature/editor was required for agent success.

The current collector preserves complete two-XML equality. Startup-only bounded adjacent-pair sampling and bounded failure snapshot retention do not weaken the ordinary input protections or grant unknown-input replay. Session total remains360s including40s cleanup, device commands8s, reads20s/two attempts. Set-text has a90s call cap and115s operational admission;90s is not an observation lifetime.

## Windows resource measurement

One observer produced258 consecutive one-second samples over257s; the maximum interval was1.016s. Host, actual Worker, emulator and QEMU were observed. A prior measurement missed actual processes and had a121.734s gap; its complete claim was rejected and it was not used for the figures below.

| Metric | Value |
|---|---:|
| Idle process working-set sum, median |3654.10MiB (~3.57GiB)|
| Task process working-set sum, peak |3872.29MiB (~3.78GiB)|
| Task private commit sum, peak |4284.78MiB (~4.18GiB)|
| Observed whole-host physical delta, task peak |4010.99MiB (~3.92GiB)|
| QEMU working set, peak |3647.03MiB|
| QEMU private commit, peak |4154.77MiB|

At the corresponding total peaks, QEMU contributed about93.88% of working set and96.70% of private commit. These are same-sample shares, not ratios of unrelated maxima. Guest configuration was1536MiB RAM/two vCPUs; this is not Windows memory consumption. The Python host/worker were a small share of the observed total, so further Python cleanup is unlikely to address the main memory cost.

Working-set sums may count shared pages more than once. Private commit is not exclusive physical RAM. Whole-host deltas include other applications and caches; they are not all causally assigned to QEMU. Idle is a short10-sample window, not a long-term idle/stability result. After Stop, the tracked instances were absent and the independent device/process checks were clean; the whole-host used-memory median was105.955MiB below baseline. This does not prove leak freedom or a particular cache/driver mechanism. One-second sampling can miss shorter peaks. All current results remain finite, single-machine observations.

## Evidence/publication boundary

The public tree provides source, a prebuilt project collector JAR with matching pinned hash, offline tests and this curated summary. SDKs/APKs/images, credentials, raw device XML/logs, installation receipts and local paths are not published. Private raw evidence hashes identify the reviewed dataset; hashes alone do not make private evidence independently reproducible:

- Resource samples SHA256:5d8cd5418ec9772eba48006f447695f51d1326b4ddcd2b26d150f931f2d9bb60
- Resource postflight SHA256:a9de04ae13dd0704bb02d7ecb473c3964e595f1366044b6982ed5da5d45066ed

Public acceptance stays false; a new checkout must perform its own trusted configuration and acceptance. The historical human_foreground_input gate is no longer a prerequisite for generic agent-product acceptance; full component hashes, required AI/refresh evidence and fail-closed receipts remain. Historical snapshots/research retain their dates and are not current completion claims.

Next: inspect actual QEMU runtime parameters and propose one minimal memory-reduction candidate. No configuration reduction or comparative improvement has been implemented or established in this milestone.
