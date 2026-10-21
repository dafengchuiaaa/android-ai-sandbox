# Bounded external-agent sessions

The worker executes one explicitly supplied formal request at a time. It does not contain an LLM or a fixed UI sequence. A real external agent supplies each decision based on the latest committed response and full observation. Native modes are `stepwise_input` and `application_refresh`.

-360s total including40s cleanup;16 CLI calls;20s reads/two attempts and8s device commands.
- Input demonstration: one launch, one click, one set-text (Aurora only), independent same-field text/focus readback.90s set-text call cap and115s operational admission are distinct.
- Refresh mode: limited repository navigation, official pinned identity, no repeated refresh dispatch, independent progress/completion observation and canonical network evidence. A narrowly proven no-input navigation-stale recovery exists only within its guarded refresh contract; it is not general retry permission.
- Exact latest full XML/response hash, unique node/valid field/focus, owned instance and unknown-input non-replay remain mandatory. Failures claim their quotas and cannot silently retry.
- The independent worker owns timeout and normal Stop even while the external agent is reasoning. Host reference code handles final evidence, cleanup and exact rollback. No human test editor/signature is required.

`begin` needs explicit project/ADB/manifests/output and optional mode; `submit` needs the current output, action/reason and action-specific observation/node/value. This repository does not include a trusted installation manifest for the reader's machine. Do not copy a private run's output/lock or invent future node IDs. See [validation scope](milestone-20261022.md) before deployment.
