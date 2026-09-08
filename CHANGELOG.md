# Changelog

## Unreleased

### Changed

- Dual-microphone AGC uses Espressif's public WebRTC processor on complete
  post-AFE mono frames. The 160-sample processing cadence adds 10 ms of fixed
  causal latency, and AGC setting changes still rebuild the AFE.

### Fixed

- Dual-microphone profiles no longer silently lose AGC when ESP-SR 2.5.3
  accepts `agc_init` but omits the stage from its effective AEC/BSS/VAD graph.
- A configured initial `vad_enabled: false` is now applied after the GMF
  element creates its wake-state lock. Disabled VAD events are ignored, so
  they no longer publish misleading speech/silence transitions.

## 2026.9.0, 2026-08-29

This release keeps the existing YAML contract compatible with the 2026.8.0
device profiles. No configuration migration is required.

### Added

- `esp_afe.output_prebuffer_frames` can retain a bounded number of processed
  dual-microphone frames before publishing output. It is disabled by default.
- Corruption counters and focused diagnostics can be compiled in when tracing
  AFE ring ownership without adding normal runtime logging to the hot path.

### Changed

- The audio task is allocated once, parked while idle and reused for the device
  lifetime. I2S completion and shutdown now have one explicit owner.
- Asynchronous AFE output frames remain valid until the consumer has finished
  with them, preventing a later producer cycle from overwriting live audio.
- ESPHome automation callbacks use the current callback-based component
  contract and can be declared more than once.
- The implementation and dependencies are aligned with ESPHome 2026.8 and
  ESP-IDF 5.5.5.

### Fixed

- Stop and restart no longer leave stale I2S completion state attached to a
  later playback cycle.
- TX completion desynchronization now stops the affected stream cleanly instead
  of allowing corrupted playback state to propagate.
