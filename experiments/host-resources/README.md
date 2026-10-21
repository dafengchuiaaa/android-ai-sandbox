# Windows resource observer

`sampler.py` is a read-only Python-standard-library/Win32 observer and offline analyzer. It records whole-host physical memory, process working set/private commit and CPU; it binds the local Host/Worker/emulator process identities from the execution directory. It is Windows-specific. This snapshot does not start an Android instance.

The execution directory and its trusted process/session files must be supplied locally. `python -B sampler.py` prints its command syntax without starting sampling. No private live dataset or device runner is distributed here. Do not interpret missing PID coverage or sampling gaps as zero resource use; inspect the analyzer's partial/complete diagnostics.

Working-set sums can double-count shared pages. Private Bytes is commit, not resident physical RAM. Whole-host deltas include unrelated applications and caches. One-second samples cannot measure subsecond peaks or prove the absence of leaks. See ../../docs/milestone-20261022.md for one scoped measurement and its limits.
