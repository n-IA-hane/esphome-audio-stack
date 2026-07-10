# esp_aec

Standalone Espressif AEC (Acoustic Echo Cancellation) wrapper for ESPHome.

## Contents
- [Overview](#overview)
- [When to use `esp_aec` vs `esp_afe`](#when-to-use-esp_aec-vs-esp_afe)
- [Quick start](#quick-start)
- [Configuration options](#configuration-options)
- [AEC modes](#aec-modes)
- [Public C++ API](#public-c-api)
- [Runtime mode switching from Home Assistant](#runtime-mode-switching-from-home-assistant)
- [Threading model](#threading-model)
- [Memory footprint](#memory-footprint)
- [Dependencies](#dependencies)
- [Known constraints](#known-constraints)
- [Troubleshooting](#troubleshooting)

## Overview

Wraps `espressif/esp-sr`'s AEC primitive and exposes it through the
`AudioProcessor` interface. Use it when the device only needs echo cancellation
on the mic path and does not need the wider AFE pipeline that `esp_afe`
provides (noise suppression, Speech Enhancement, VAD, AGC).

## When to use `esp_aec` vs `esp_afe`

| Scenario | Pick |
|----------|------|
| Single mic, echo cancellation only | `esp_aec` |
| Memory-constrained full-duplex voice device where NS/AGC/VAD are not required | `esp_aec` |
| Codec/TDM board that needs the full ESP-SR AFE pipeline | `esp_afe` |
| Voice Assistant + dual-mic with Speech Enhancement | `esp_afe` |
| Need noise suppression or AGC on the mic path | `esp_afe` |

Both components implement `AudioProcessor` at the type level, but `esp_afe` is only safely usable behind `esp_audio_stack`. See the root [ESPHome Audio Stack README](../../../README.md) for the topology matrix.

## Quick start

```yaml
external_components:
  - source:
      type: git
      url: https://github.com/n-IA-hane/esphome-audio-stack
      ref: main
    components: [esp_audio_stack, esp_aec]

esp_aec:
  id: aec_processor
  sample_rate: 16000
  filter_length: 4
  mode: sr_low_cost

esp_audio_stack:
  id: audio_stack
  processor_id: aec_processor
  # ... (see esp_audio_stack README for the rest)
```

## Configuration options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `id` | ID | required | Component identifier referenced by `esp_audio_stack.processor_id`. |
| `sample_rate` | int | 16000 | Must match the sample rate of the consumer. esp-sr's AEC only accepts 16 kHz frames; the upstream component is expected to rate-convert from the I²S bus rate when needed. |
| `filter_length` | int | 4 | ESP-SR filter-length parameter, range 1 to 8. The library supplies the frame shape for each engine at runtime, so do not treat one value as a universal millisecond duration. Increase only after measuring echo-tail coverage and heap headroom on the target. |
| `mode` | string | `sr_low_cost` | AEC algorithm. Pick the engine to match the use case: **FD modes** for full-duplex codec devices where speaker echo is audible, **SR modes** where wake-word spectral preservation matters more than residual echo suppression, **VOIP modes** for real-time call audio. |

## AEC modes

The resolved ESP-SR 2.4.x line supplies SR, VOIP and FD AEC modes:

| Mode | Engine shape | Non-linear suppression | Starting point |
|------|--------------|------------------------|----------------|
| `sr_low_cost` | Speech-recognition linear AEC | No | Voice Assistant/MWW when preserving recognition features matters. |
| `sr_high_perf` | Higher-cost SR/FFT variant | No | SR experiments with verified contiguous DMA-capable heap. |
| `voip_low_cost` | Voice-communication AEC | Yes | Call-focused enclosure where stronger residual suppression is useful. |
| `voip_high_perf` | Higher-cost voice-communication variant | Yes | Call-focused target with measured CPU and heap headroom. |
| `fd_low_cost` | Full-duplex AEC | Yes | Codec-backed two-way speech with audible speaker echo. |
| `fd_high_perf` | Higher-cost full-duplex variant | Yes | Same use case after target qualification and heap preflight. |

Why `sr_*` is recommended for VA + MWW: the SR engines use a linear-only
adaptive filter that better preserves spectral features used by wake-word
models. VOIP engines add residual echo suppression that can reduce detection
accuracy, especially during playback.

The exact CPU cost, residual echo and wake-word hit rate depend on the board,
model, enclosure, playback level and resolved ESP-SR build. Treat mode names as
selection guidance, then record repeatable target measurements; historical
small-sample detection counts are not product guarantees.

`sr_high_perf` allocates a contiguous DMA-capable internal block at switch time. The component runs a pre-flight heap check and refuses the switch cleanly (logs a warning, keeps the previous mode active) if the block is not available.

## Public C++ API

| Method | Purpose |
|--------|---------|
| `setup()` / `dump_config()` / `get_setup_priority()` | Standard ESPHome component lifecycle. |
| `bool is_initialized() const` | True when the esp-sr AEC handle is ready. |
| `FrameSpec frame_spec() const` | Frame size and channel layout that `process()` expects. |
| `bool process(mic, ref, out, mic_channels)` | Run one AEC frame. Returns false if the handle is not ready. |
| `FeatureControl feature_control(AudioFeature)` | `AEC` is `RESTART_REQUIRED`; everything else is `NOT_SUPPORTED`. |
| `bool set_feature(AudioFeature, bool enabled)` | Currently returns `false`; standalone AEC has no per-feature live toggle. Use a mode reconfigure or the parent stack's explicit processor bypass. |
| `ProcessorTelemetry telemetry() const` | Returns the default/empty telemetry record; standalone AEC does not publish frame or ring counters. |
| `bool reconfigure(int type, int mode)` | Switch AEC mode by numeric code. `type` selects the engine (0 = SR, 1 = VC, 2 = FD); `mode` selects the variant (0 = LOW_COST, 1 = HIGH_PERF). Prefer the YAML action `esp_aec.set_mode` for automations. |
| `bool reinit_by_name(const std::string &name)` | Switch AEC mode by name (`"sr_low_cost"` etc.). Recommended entry point. Returns false on rejection (for example, when `sr_high_perf` cannot allocate DMA). |
| `std::string get_mode_name() const` | Current mode as a string. Read this after `reinit_by_name` to confirm the switch was accepted. |

## Runtime mode switching from Home Assistant

The `AEC Mode` select wires runtime modes to a Home Assistant select entity, with the device publishing back the live mode so a rejected switch never leaves HA showing the wrong value.

**Engine standard**: stay inside one engine family per device to avoid esp-sr's silent FFT calloc-fail bug on cross-engine transitions at `filter_length > 4`.

VoIP-only (no MWW) - VOIP engine only:

```yaml
select:
  - platform: template
    id: aec_mode_select
    name: "AEC Mode"
    options:
      - "voip_low_cost"
      - "voip_high_perf"
    initial_option: "voip_high_perf"
    optimistic: false
    restore_value: true
    set_action:
      - esp_aec.set_mode:
          id: aec_processor
          mode: !lambda 'return x;'
      - lambda: 'id(aec_mode_select).publish_state(id(aec_processor).get_mode_name());'
```

Voice Assistant device with wake word and codec loopback where echo is audible - SR plus FD choices:

```yaml
select:
  - platform: template
    id: aec_mode_select
    name: "AEC Mode"
    options:
      - "sr_low_cost"
      - "sr_high_perf"
      - "fd_low_cost"
      - "fd_high_perf"
    initial_option: "fd_low_cost"
    optimistic: false
    restore_value: true
    set_action:
      - esp_aec.set_mode:
          id: aec_processor
          mode: !lambda 'return x;'
      - lambda: 'id(aec_mode_select).publish_state(id(aec_processor).get_mode_name());'
```

`optimistic: false` matters: without it, `template_select::control()` auto-publishes the user-selected value after the action runs, overwriting any rejection (e.g. when `sr_high_perf` cannot allocate the contiguous DMA block).

## Threading model

None. `esp_aec::process()` runs synchronously on the caller's audio task. The caller (typically `esp_audio_stack`'s audio task on Core 0) owns the realtime thread. There are no internal worker tasks, no FreeRTOS objects beyond a mutex around mode-switch reinit.

## Memory footprint

ESP-SR owns most mode-dependent allocations, and their internal/PSRAM split can
change with the resolved library, target and filter length. High-performance
modes additionally require a large contiguous DMA-capable internal block; the
component checks the largest block before rebuilding and retains the previous
mode when the check fails. Measure free, minimum-free and largest internal
blocks on the final composite firmware instead of budgeting from fixed totals
quoted for another board.

## Dependencies

- ESP32 only, restricted to S3 and P4 variants (enforced in `_validate_esp32_variant`).
- Resolves `espressif/esp-dsp` with `^1.8.0` and `espressif/esp-sr` with
  `^2.4.6` through ESPHome's IDF component manager, then wraps the low-level
  `afe_aec` helper. The constraints allow a newer compatible release, so the
  lockfile/build manifest is the authority for an individual firmware.
- Implements [`AudioProcessor`](../esp_audio_stack/README.md), so it can be referenced by any component that accepts that interface.

## Logging

The component logs under the tag `esp_aec`.

- `WARN` - `sr_high_perf` pre-flight rejected (contiguous DMA-capable internal RAM unavailable), handle rebuild failed mid-call
- `INFO` - `Reinitializing AEC: mode X -> Y`, `AEC reinitialized: mode=N, frame=M` (mode-switch lifecycle)
- `DEBUG` - frame-level processor_->process() trace (rare, mostly for esp-sr binding development)

To mute AEC chatter without losing project-wide DEBUG: `logger.logs.esp_aec: INFO`.

## Known constraints

- Sample rate is fixed at 16 kHz (the rate esp-sr's AEC expects). When the I²S bus runs faster, the upstream component must rate-convert; `esp_audio_stack` does this with Espressif's `esp_ae_rate_cvt`.
- Mode changes (`sr_low_cost` vs `sr_high_perf` vs `voip_*` vs `fd_*`) require a handle rebuild, which causes a short audio gap. Do not change mode while real-time audio is streaming.
- `filter_length` is configured from YAML and retained across runtime mode
  rebuilds. Longer filters may improve echo-tail coverage at the cost of CPU
  and memory.
- The `sr_high_perf` mode needs a contiguous DMA-capable internal allocation. On a fragmented heap the pre-flight check refuses the switch and logs a warning; the device keeps running on the previous mode.

## Troubleshooting

**Echo cancellation does nothing.**
Make sure `esp_audio_stack` actually links `processor_id: aec_processor`. The
component initialises silently even when nobody references it.

**The far end still hears the speaker on a codec-backed full-duplex device.**
Use `fd_low_cost` first. For ES8311 digital feedback, verify
`no_dac_ref: false`, `use_stereo_aec_reference: true` and
`reference_channel: right`, because Espressif's ES8311 driver outputs
`ADCL + DACR` in this mode.

**Wake word detection rate dropped after enabling AEC.**
You are likely on a `voip_*` mode. Switch to `sr_low_cost`. The VOIP engines apply a residual echo suppressor that distorts the features the MWW model expects.

**`sr_high_perf` switch fails at runtime.**
The pre-flight check on contiguous DMA-capable internal RAM rejected the switch. Free internal heap by enabling `buffers_in_psram: true` on `esp_audio_stack`, or stay on `sr_low_cost`. Check `heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT)` in the logs.

**AEC mode select in Home Assistant shows the wrong value after a switch attempt.**
You are missing `optimistic: false` on the template select. Without it, the template auto-publishes the user-selected value over the live mode the device actually applied.

## License

The ESPHome wrapper code is MIT-licensed. ESP-SR, ESP-DSP and other fetched
Espressif dependencies retain their own licenses and product-use restrictions;
see the repository `THIRD_PARTY_NOTICES.md`.
