# Changelog

## ESPHome Audio Stack 2026.10.1-dev

- Toggle single-microphone AEC through the existing Espressif GMF control, keeping the active audio pipeline and frame sizes intact instead of rebuilding it during a call.
- Add an on-demand runtime diagnostic action that works without verbose audio tracing.
- Exercise failure cleanup and retries with behavioral tests.
- Clear effective codec layouts when the backend closes, so a failed reopening cannot expose an old valid layout.

## ESPHome Audio Stack 2026.10.0

This release improves microphone and playback handling while keeping the normal ESPHome microphone and speaker interfaces. Capture and playback can run together on one shared I2S bus; a second bus is optional.

### What improves

- TDM boards use less DMA memory by storing only the selected microphone, reference and playback slots. Physical wiring, bus rate and YAML slot numbers stay the same.
- One- and two-microphone configurations feed the AFE with the configured channel order. Both standard-I2S microphone pairs and supported TDM arrangements remain available.
- AFE input is supplied in bounded blocks, so a larger speech-processing frame does not dictate the hardware audio processing interval.
- Voice-communication AEC modes reserve the feed-task stack they need.
- WebRTC noise-suppression builds avoid linking unused neural-model weights into internal RAM.
- The documentation now explains simple I2S audio, shared-bus duplex, codec feedback, TDM, AEC and AFE in order, with diagrams and configuration examples.

### Updating

Use **ESPHome 2026.9.0 or newer** with the maintained profiles. Rebuild and upload firmware to receive these changes; updating a Home Assistant integration does not update the device's audio backend.

Keep existing physical TDM slot numbers. Standard component configurations do not need a manual codec-library declaration. This release selects Espressif `esp_codec_dev` **2.0.0-beta5**; that dependency is still an upstream prerelease. Remove custom 1.x overrides before building because the backend now uses the 2.x API.

Audio Stack can be used independently of VoIP Stack. Microphone-only and speaker-only configurations remain supported where the hardware permits them. Echo cancellation and AFE processing are optional.

### Validation

Full-profile concurrency was exercised on Waveshare S3 Audio and Spotpear, including direct ESP calls. These results apply to the tested boards and configurations, not every possible codec board or enclosure. The software suite passes 68 tests, and the documentation examples were checked through ESPHome configuration validation.

### Thanks

Thanks to @jyoushiki for the sparse-TDM proposal, detailed measurements and hardware testing that helped shape this improvement.

Thanks to everyone who donated to support the project.

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
