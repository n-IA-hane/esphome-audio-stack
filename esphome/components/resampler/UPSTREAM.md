# Resampler scheduling for full audio profiles

Based on the ESPHome 2026.8.2 installed component. The C++ files match
ESPHome commit `b9eda644cd0219e560933b5151c0912f4c648278` before this patch.
The Python schema differs from that commit only in type annotations.

This adapter retains the upstream resampling algorithm, buffers and speaker
lifecycle. It exposes `task_core` (0 or 1, unpinned when omitted) and
`task_priority` (1 to 23, default 1). It requires `esp_audio_stack` and reuses
its pinned-task creation primitive, with ESP-IDF stack sizes in bytes.
The same task owns all resampling work and is released only after its stopped
notification, as in the upstream lifecycle.

On dual-core ESP32 chips, floating-point work can pin an unbound task to the
core where it first runs. Full profiles can select a core explicitly to keep
resampling from competing with microphone DSP. These settings do not change
sample rates or add audio buffering.

The upstream C++ files retain their GPLv3 license; the Python schema retains
its MIT license. The original ESPHome license is included in `LICENSE`.

The development adapter also accepts bounded prefixes of PCM blocks larger
than its input buffer and waits for space during backpressure. These changes
are separate from task placement. Retiring this provider requires preserving
large-block progress and stop/reset behavior as well as the scheduling options.
