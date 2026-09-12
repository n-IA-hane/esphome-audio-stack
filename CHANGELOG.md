# Changelog

## Development

- TDM RX and TX now transfer only their configured active slots through DMA
  while retaining the complete physical slot count for BCLK/WS timing. This
  reduces internal DMA memory on sparse layouts such as dual microphones plus
  an echo-reference input and a single playback slot.
- The new `processor_dma_margin` option can disable the automatic 25% TDM
  processor-frame headroom on memory-constrained targets. It defaults to
  enabled, preserving existing configurations.

---

## ESPHome Audio Stack 2026.9.2

More reliable audio startup and firmware updates, with optional dual-microphone features.

- Microphone capture can be requested before setup without crashing.
- Microphone gain controls work without declaring an unused speaker.
- I2S completion handling remains safe during flash writes and OTA confirmation on P4.
- Two standard-I2S microphone slots can feed the existing dual-microphone AFE.
- Optional slot-level sensors expose each input's level without changing the mono microphone output. Left/right identifies I2S slots; physical position depends on wiring.
- Dual-microphone processing honors VAD and offers optional gain normalization. Changing AGC can briefly restart processing.

Existing profiles do not enable optional features automatically. Rebuild and upload firmware to receive these changes.

Thanks to @jharris4, @benklop and @jyoushiki for their contributions, and @TheEris and @catthetech for hardware feedback.

---

## ESPHome Audio Stack 2026.9.1

This stable release accompanies ESPHome Intercom 2026.9.1 and improves audio processing during calls and simultaneous assistant or music use.

### Improvements

- AFE processing uses the same GMF path for single- and dual-microphone configurations.
- Partial processed audio frames are retained instead of losing samples between reads.
- Starting, pausing and restarting processing handles inactive microphone input more reliably.
- Audio processing preserves frame cadence when AEC and related features change.
- The speaker resampler adapter exposes task core and priority settings for the maintained device profiles.

Use the matching maintained Intercom profile for your device. This release does not require changing your selected audio format.

[Platform release notes and update instructions](https://github.com/n-IA-hane/esphome-intercom/releases/tag/v2026.9.1)


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
