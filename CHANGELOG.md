# Changelog

## 2026.10.0-dev: consolidated microphone layouts and AFE processing

This development preview accompanies Intercom 2026.10.0-dev. Use ESPHome 2026.9.0 or newer with the maintained profiles.

- Microphone and reference-channel layouts follow the configured inputs, including supported single- and dual-microphone arrangements.
- AFE input is delivered in bounded processing blocks.
- Voice-communication echo cancellation reserves the feed-task stack it needs.
- WebRTC-based AFE builds avoid loading unused neural noise-suppression models.

The public ESPHome microphone and speaker interfaces remain available. Audio-only, microphone-only and speaker-only configurations remain supported where their hardware permits them.

Full-profile concurrency was exercised on Waveshare S3 Audio and Spotpear, including direct ESP calls. The latest package/controller work adds no new changes to this audio backend.

Rebuild and upload firmware to receive component changes. See the [platform preview](https://github.com/n-IA-hane/esphome-intercom/releases/tag/v2026.10.0-dev) for the shared playback and controller improvements.

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
