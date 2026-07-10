# ESP AFE - Full Audio Front-End Pipeline

> ⚠ **Important: `esp_afe` requires `esp_audio_stack` in front of it.** The AFE
> pipeline expects a steady 16 kHz frame cadence using the frame shape reported
> by ESP-SR. Use it as the processor behind `esp_audio_stack`, not as an
> arbitrary standalone filter.

ESPHome component wrapping Espressif's **ESP-SR AFE** (Audio Front End)
through two target-specific paths: single-mic profiles call the ESP-SR AFE
feed/fetch interface directly, while dual-mic profiles use the official
`esp_gmf_afe` element and GMF manager/pipeline tasks. The component provides
AEC, Speech Enhancement on dual-mic targets, optional NS/VAD/AGC stages,
runtime controls and diagnostic sensors.
Supports single-mic (MR) and dual-mic (MMR/MMNR) configurations.

## Overview

`esp_afe` uses the closed-source `esp-sr` library's AFE pipeline, which chains multiple DSP stages depending on configuration:

**Single-mic (MR) mode** (`mic_num: 1`):

```text
[mic + ref] -> |AEC| -> |NS| -> |VAD| -> |AGC| -> [clean output]
```

**Dual-mic (MMR/MMNR) mode** (`se_enabled: true` with `mic_num: 2`):

```text
[mic1 + mic2 + ref] -> |AEC| -> |Speech Enhancement| -> [clean output]
```

> **Note**: When Speech Enhancement is active, esp-sr prioritizes BSS over NS.
> `afe_config_check()` may clear `ns_init` on dual-mic builds. AGC can be
> configured at boot, but the stock GMF manager does not expose it as a live
> feature, so AGC changes require AFE reinit.

Unlike `esp_aec` (standalone echo cancellation only), `esp_afe` provides a full
signal processing pipeline. Both components implement the `AudioProcessor`
interface, but the AFE feed/fetch task model needs the steady producer that
`esp_audio_stack` provides.

### When to use esp_afe vs esp_aec

| Feature | esp_aec | esp_afe |
|---------|---------|---------|
| Echo Cancellation | Yes | Yes |
| Speech Enhancement | No | Yes (dual-mic) |
| Noise Suppression | No | Yes (WebRTC, single-mic mode) |
| Voice Activity Detection | No | Yes (WebRTC) |
| Automatic Gain Control | No | Yes (WebRTC, when kept by `afe_config_check`) |
| Runtime switches in HA | Parent-stack processor bypass | AEC and VAD live through the active AFE API; NS/AGC by AFE reinit; SE/BSS is structural |
| Diagnostic sensors | No | Input volume, output RMS, voice presence |
| Runtime shape | Synchronous on the parent audio task | Direct feed/fetch for one mic; GMF manager/pipeline for two mics |
| Relative footprint | Lower | Higher and target/graph dependent |
| Supported platforms | ESP32-S3, ESP32-P4 | ESP32-S3, ESP32-P4 |

**Choose `esp_aec`** when you need minimal RAM usage and only echo cancellation.
**Choose `esp_afe`** when you want Espressif AFE processing, VAD, optional NS/AGC, and diagnostic sensors.

## Requirements

- **ESP32-S3** or **ESP32-P4** with PSRAM
- ESP-IDF framework
- `esp_audio_stack` in front of the processor.

## Installation

```yaml
external_components:
  - source:
      type: git
      url: https://github.com/n-IA-hane/esphome-audio-stack
      ref: main
    components: [esp_audio_stack, esp_afe]
```


## Configuration

### Basic Setup

```yaml
esp_afe:
  id: afe_processor
  type: sr
  mode: low_cost

esp_audio_stack:
  id: audio_stack
  # ... pins ...
  processor_id: afe_processor
```

### Complete Configuration

```yaml
esp_afe:
  id: afe_processor
  type: sr                    # sr, vc (voice communication), or fd (full duplex)
  mode: low_cost              # low_cost or high_perf
  mic_num: 2                  # Number of microphones (1 or 2)
  se_enabled: true            # Speech Enhancement, requires mic_num: 2
  aec_enabled: true           # Echo cancellation
  aec_filter_length: 4        # ESP-SR filter-length parameter (1-8)
  aec_nlp_level: aggressive   # normal, aggressive, or very_aggressive
  ns_enabled: true            # Noise suppression (WebRTC)
  vad_enabled: false          # Voice activity detection
  vad_mode: 3                 # VAD aggressiveness (0-4, higher = more aggressive)
  vad_min_speech_ms: 128      # Min speech duration to trigger VAD
  vad_min_noise_ms: 1000      # Min noise duration before VAD clears
  vad_delay_ms: 128           # VAD state transition delay
  agc_enabled: true           # Automatic gain control (WebRTC)
  agc_compression_gain: 9     # AGC compression gain (0-30 dB)
  agc_target_level: 3         # AGC target level (0-31, lower = louder)
  memory_alloc_mode: more_psram  # Memory allocation strategy
  afe_linear_gain: 1.0        # Linear gain applied to output (0.1-10.0)
  task_core: 1                # esp-sr SE/BSS worker core preference
  task_priority: 5            # esp-sr SE/BSS worker priority
  ringbuf_size: 8             # Internal ring buffer size in frames (default 8)
  feed_task_core: 0           # Dual-mic GMF manager feed task core
  feed_task_priority: 5       # Dual-mic GMF manager feed task priority
  feed_task_stack_size: 3072  # Dual-mic GMF manager feed task stack
  fetch_task_core: 1          # Dual-mic GMF fetch and pipeline task core
  fetch_task_priority: 5      # Dual-mic GMF fetch/pipeline priority
  fetch_task_stack_size: 3072 # Dual-mic GMF fetch/pipeline stack
```

### Configuration Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `id` | ID | Required | Component ID |
| `type` | string | `sr` | AFE type: `sr` (speech recognition), `vc` (voice communication) or `fd` (full-duplex AFE, esp-sr 2.4+) |
| `mode` | string | `low_cost` | AFE mode: `low_cost` or `high_perf` |
| `mic_num` | int | `1` | Number of microphones (1 or 2). Dual-mic configs must enable `se_enabled`; SE/BSS is structural for two-mic AFE |
| `aec_enabled` | bool | **true** | Enable acoustic echo cancellation |
| `aec_filter_length` | int | `4` | ESP-SR filter-length parameter (1-8). Effective time coverage depends on the selected engine/frame shape; tune it from measured echo-tail behavior. |
| `aec_nlp_level` | string | `aggressive` | ESP-SR nonlinear echo suppression level: `normal`, `aggressive`, or `very_aggressive`. Lower levels preserve near-end wake speech better while playback is active; higher levels suppress speaker leakage harder |
| `ns_enabled` | bool | **true** | Enable noise suppression (WebRTC engine) |
| `agc_enabled` | bool | **true** | Enable automatic gain control (WebRTC engine) |
| `se_enabled` | bool | **false** | Enable Speech Enhancement / spatial source separation. Required for `mic_num: 2`; dual-mic AFE treats SE/BSS as structural and does not expose a runtime SE switch |
| `vad_enabled` | bool | **false** | Enable voice activity detection |
| `vad_mode` | int | `3` | VAD aggressiveness (0-4). Higher = rejects more noise but may miss quiet speech |
| `vad_min_speech_ms` | int | `128` | Minimum speech duration to trigger voice detection (32-60000 ms) |
| `vad_min_noise_ms` | int | `1000` | Minimum noise duration before VAD clears (64-60000 ms) |
| `vad_delay_ms` | int | `128` | VAD state transition delay (0-60000 ms) |
| `vad_mute_playback` | bool | `false` | When VAD detects speech, mute the speaker output to prevent acoustic feedback during voice commands. Useful for voice-assistant pipelines that play TTS while still listening. |
| `vad_enable_channel_trigger` | bool | `false` | Per-channel VAD triggering (multi-mic setups). esp-sr exposes which mic channel detected the speech, useful for Speech Enhancement-aware downstream consumers. |
| `continuous_vad` | bool | `false` | Allow VAD to keep the microphone/AFE path active without an external consumer. Use `true` when the `Voice Detected` binary sensor must work in standby; keep `false` when another consumer should own the mic lifecycle. |
| `agc_compression_gain` | int | `9` | AGC compression gain in dB (0-30) |
| `agc_target_level` | int | `3` | AGC target level (0-31, lower value = louder output) |
| `memory_alloc_mode` | string | `more_psram` | Memory allocation: `more_internal`, `internal_psram_balance`, `more_psram` |
| `afe_linear_gain` | float | `1.0` | Linear gain multiplier applied to output (0.1-10.0) |
| `task_core` | int | `1` | Core preference for the esp-sr SE/BSS worker task created by the AFE instance. |
| `task_priority` | int | `5` | Priority for the esp-sr SE/BSS worker task. |
| `ringbuf_size` | int | `8` | Requested ESP-SR internal ring size in frames (2-32). The direct single-mic path normalizes values below 16 to 16 for stable feed/fetch cadence. Larger values trade memory and latency for tolerance. |
| `feed_task_core` | int | `0` | Dual-mic GMF manager feed-task core. |
| `feed_task_priority` | int | `5` | Dual-mic GMF manager feed-task priority. |
| `feed_task_stack_size` | int | `3072` | Dual-mic GMF manager feed-task stack size in bytes. |
| `fetch_task_core` | int | `1` | Dual-mic GMF manager fetch-task core; also used for its single-element pipeline task. |
| `fetch_task_priority` | int | `5` | Dual-mic GMF manager fetch/pipeline priority. |
| `fetch_task_stack_size` | int | `3072` | Dual-mic GMF manager fetch/pipeline stack size in bytes. |
| `feed_buf_in_psram` | bool | `false` | Place the direct-path or split-frame feed scratch buffer in PSRAM. GMF profiles whose process frame equals the feed frame write directly into the feed ring and do not allocate this buffer. |
| `feed_ring_in_psram` | bool | `false` | Place the complete-frame feed bridge ring in PSRAM. Internal is faster; PSRAM may recover internal headroom. |
| `fetch_ring_in_psram` | bool | `false` | Place the complete-frame output bridge ring in PSRAM. Internal is faster; PSRAM may recover internal headroom. |

> **Buffer placement guidance**: defaults are tuned for the fastest Core 0
> audio path. Dual-mic voice profiles usually keep
> `feed_buf_in_psram`, `feed_ring_in_psram` and `fetch_ring_in_psram` false
> when the board has enough contiguous internal/DMA heap. This avoids PSRAM
> traffic on the hot AFE bridge path. Each flag remains independent for
> board-specific tuning: enabling it recovers that buffer's internal allocation
> at the cost of PSRAM reads/writes. GMF profiles with matching
> process/feed frame sizes omit the feed buffer, reducing both memory and PSRAM
> traffic for that path.

> **Defaults are designed so that a minimal config already enables AEC + NS + AGC.** You only need to declare options that differ from the defaults. In particular:
> - `aec_enabled`, `ns_enabled`, `agc_enabled` are **true** by default. Only set them if you want to **disable** a feature.
> - `se_enabled` and `vad_enabled` are **false** by default. Set `se_enabled: true` for every dual-mic AFE target; set `vad_enabled: true` only when the product explicitly needs VAD active at boot.
> - `memory_alloc_mode` defaults to `more_psram`, SE/BSS worker defaults to `task_core: 1` / `task_priority: 5`, and the dual-mic GMF path keeps manager feed on Core 0 while manager fetch and the GMF pipeline task run on Core 1. Override only if telemetry shows task starvation or a board-specific scheduling issue.
>
> **Minimal single-mic** (AEC + NS + AGC out of the box):
>
> ```yaml
> esp_afe:
>   id: afe_processor
>   type: sr
>   mode: low_cost
> ```
>
> **Minimal dual-mic** (adds Speech Enhancement):
>
> ```yaml
> esp_afe:
>   id: afe_processor
>   type: sr
>   mode: low_cost
>   mic_num: 2
>   se_enabled: true
> ```
>
> For dual-mic voice profiles, set `agc_enabled: false` and
> use `packages/esp_afe/dual_mic_entities.yaml`, which intentionally omits the
> AGC switch. Everything else (AEC, memory, task settings) uses sensible
> defaults and does not need to be repeated unless the target needs board
> tuning.

### AFE Type and Mode

The combination of `type` and `mode` determines the AEC engine and DSP pipeline:

| type + mode | AEC shape | Recognition impact | Starting point |
|-------------|-----------|--------------------|----------------|
| `sr` + `low_cost` | Linear speech-recognition AEC | Usually preserves wake-word features best | VA + MWW + VoIP baseline |
| `sr` + `high_perf` | Higher-cost SR/FFT variant | Recognition-oriented | Test only with verified contiguous internal heap |
| `vc` + `low_cost` | Communication AEC with residual suppression | May reduce wake-word accuracy | Call-focused target without MWW |
| `vc` + `high_perf` | Higher-cost communication variant | May reduce wake-word accuracy | Call-focused target with measured headroom |
| `fd` + `low_cost` | Full-duplex/NLP pipeline | Target dependent | Two-way speech baseline |
| `fd` + `high_perf` | Higher-cost full-duplex variant | Target dependent | Qualified target with measured headroom |

> **Starting guidance**: use `sr` + `low_cost` for Voice Assistant + MWW.
> Communication-oriented residual suppression can change the spectral features
> a wake-word model consumes. Record repeatable target tests before selecting a
> different engine; CPU and detection rates are not portable across builds,
> enclosures and playback levels.

NS and AGC always use the WebRTC engine regardless of type/mode. They work at boot, but the stock GMF AFE manager does not publish NS/AGC feature toggles, so this component changes them through AFE reinit (see [Feature Toggle Behavior](#feature-toggle-behavior)).

## Platform Entities

### Switch Platform

Runtime control of AFE features via Home Assistant switches:

```yaml
switch:
  - platform: esp_afe
    esp_afe_id: afe_processor
    aec:
      name: "Echo Cancellation"
      restore_mode: RESTORE_DEFAULT_ON
    ns:
      name: "Noise Suppression"
      restore_mode: RESTORE_DEFAULT_ON
    vad:
      name: "Voice Activity Detector"
      restore_mode: RESTORE_DEFAULT_OFF
    agc:
      name: "Auto Gain Control"
      restore_mode: RESTORE_DEFAULT_ON
```

| Switch | Icon | Description |
|--------|------|-------------|
| `aec` | `mdi:ear-hearing` | Echo cancellation toggle through the active direct/GMF AFE control API (live, no rebuild) |
| `ns` | `mdi:volume-off` | Noise suppression toggle (requires AFE reinit and an audible gap). Use only on single-mic AFE builds; esp-sr prioritizes SE/BSS over NS on dual-mic input |
| `vad` | `mdi:account-voice` | Voice activity detection toggle. VAD is structurally initialized and enabled/disabled through the active direct/GMF AFE control API |
| `agc` | `mdi:tune-vertical` | Auto gain control toggle (requires AFE reinit). Use only on single-mic or custom diagnostic builds whose checked runtime config keeps `agc_init: true`; public dual-mic packages omit it |

Use `RESTORE_DEFAULT_OFF` for VAD restore on devices where wake word or another
microphone consumer owns the capture path:
VAD is off on first boot, but the user's HA switch state is preserved after
that. If `Voice Detected` should keep working in standby, set
`continuous_vad: true` so the background mic path is intentional.

Dual-mic packages keep the feed sent to ESP-SR fixed: both microphone channels
plus the playback reference are always present. The public bridge consumes the
official `esp_gmf_afe` output port, so Espressif owns the manager callbacks and
internal output buffer. That element path does not expose
`afe_fetch_result_t::raw_data[n]`; keep AEC enabled by default on dual-mic
SE/BSS boards because AEC-off output may sound metallic.

```text
TDM / codec input
  mic 1 slot --------------+
  mic 2 slot --------------+--> esp_audio_stack --> ESP-SR feed frame
  speaker reference slot --+                         M M [N] R
                                                         |
                                                         v
                                                ESP-SR AFE pipeline
                                                SE/BSS + AEC/VAD
                                                         |
                                                         v
                                                AFE fetch result
                                    official data or configured raw_data[n]
                                                         |
                                            ESP-SR target mono output
                                                         |
                                                         v
                                             MWW / Voice Assistant / VoIP
```

### Binary Sensor Platform

```yaml
binary_sensor:
  - platform: esp_afe
    esp_afe_id: afe_processor
    vad:
      name: "Voice Presence"
      update_interval: 100ms
```

| Sensor | Device Class | Description |
|--------|-------------|-------------|
| `vad` | `sound` | Voice activity state. ON when speech detected, OFF when noise/silence. Requires `vad_enabled: true` in config |

### Sensor Platform

```yaml
sensor:
  - platform: esp_afe
    esp_afe_id: afe_processor
    input_volume:
      name: "Input Volume"
      update_interval: 250ms
    output_rms:
      name: "Output RMS"
      update_interval: 250ms
```

| Sensor | Unit | Description |
|--------|------|-------------|
| `input_volume` | dBFS | RMS level of mic input before processing. Useful for mic gain calibration |
| `output_rms` | dBFS | RMS level of processed output. Compare with input to see NS/AGC effect |

## Actions

### esp_afe.set_mode

Queue an AFE type/mode change at runtime. The action is asynchronous: it returns
after the request is queued, while a background task tears down and rebuilds the
AFE instance. On GMF dual-mic builds this can take a few hundred milliseconds,
so callers that restart audio must wait for `is_reconfigure_idle()` and check
`get_last_reconfigure_ok()`.

For full audio-stack devices, stop the audio stack first, queue the reconfigure,
wait for completion, then restart audio only if the reconfigure succeeded.

```yaml
select:
  - platform: template
    id: afe_mode_select
    name: "AEC Mode"
    options:
      - sr_low_cost
      - sr_high_perf
      - voip_low_cost
      - voip_high_perf
      - fd_low_cost
      - fd_high_perf
    initial_option: "fd_high_perf"
    optimistic: false           # do NOT auto-publish; we publish the live mode below
    restore_value: false        # HA mirrors boot config; it does not choose the boot mode
    set_action:
      - esp_audio_stack.stop_and_wait: audio_stack
      - wait_until:
          condition:
            esp_audio_stack.is_idle: audio_stack
          timeout: 2s
      - esp_afe.set_mode:
          id: afe_processor
          mode: !lambda 'return x;'
      - wait_until:
          condition:
            lambda: 'return id(afe_processor).is_reconfigure_idle();'
          timeout: 20s
      - if:
          condition:
            lambda: 'return id(afe_processor).is_reconfigure_idle() && id(afe_processor).get_last_reconfigure_ok();'
          then:
            - esp_audio_stack.start: audio_stack
          else:
            - logger.log:
                level: ERROR
                format: "AFE mode switch did not complete cleanly; audio restart skipped"
      - lambda: 'id(afe_mode_select).publish_state(id(afe_processor).get_mode_name());'
```

Valid mode strings: `sr_low_cost`, `sr_high_perf`, `voip_low_cost`, `voip_high_perf`, `fd_low_cost`, `fd_high_perf`.

`get_mode_name()` returns the live mode as a string after the reinit. The
`optimistic: false` plus the explicit `publish_state()` at the end is the
recommended pattern: it stops `template_select::control()` from auto-publishing
the user-selected value over a rejected switch (e.g. when a high-performance mode
cannot allocate the contiguous DMA-capable internal block).

## Feature Toggle Behavior

AEC, VAD, and NS/AGC toggle differently because the selected direct/GMF path
exposes only part of the lower ESP-SR runtime control surface:

| Feature | Toggle Method | Audio Gap | Notes |
|---------|-------------|-----------|-------|
| AEC | Live AFE control | None | Immediate on/off through the direct API or `ESP_AFE_FEATURE_AEC` on GMF |
| SE | Boot-time graph choice | N/A | Structural on dual-mic builds; single-mic users should use a single-mic config or `esp_aec` |
| NS | AFE reinit | hundreds of ms plus possible audio-task restart | ESP-SR exposes low-level vtable entries, but `esp_gmf_afe_manager` does not expose an NS feature enum. Not exposed on dual-mic SE/BSS builds because `afe_config_check()` prioritizes BSS over NS |
| AGC | AFE reinit | hundreds of ms plus possible audio-task restart | Same manager limitation as NS. Avoid toggling while real-time audio is active. |
| VAD | Live AFE control | None | `vad_init` stays structural; runtime on/off uses the selected direct/GMF control without rebuilding the AFE instance |

**Why reinit for NS/AGC?** ESP-SR's low-level AFE vtable includes
`enable_ns()`, `disable_ns()`, `enable_agc()`, and `disable_agc()`, but the
stock GMF manager keeps the AFE iface/data private and only publishes runtime
feature toggles for AEC, VAD, and SE that are relevant to this component.
For consistent behavior across the direct and GMF implementations, this wrapper
represents NS/AGC changes as config changes and rebuilds the AFE instance.

The reinit is safe: the previous AFE is destroyed first (ESP-SR's FFT resources are a global singleton, only one instance can exist), then the new one is built. While an AFE instance is active, missing processed output is emitted as silence rather than raw pre-AFE microphone audio.

## Architecture

```text
               AudioProcessor interface
                       |
              +--------+--------+
              |                 |
           EspAec            EspAfe
         (AEC only)    (AEC+NS+VAD+AGC)
              |                 |
              +--------+--------+
                       |
            |
    esp_audio_stack
    (processor_id)
```

Both `EspAec` and `EspAfe` implement `AudioProcessor`. `esp_audio_stack` calls
`process(mic, ref, out)` without knowing which implementation is behind it. The
supported pairings are:

| Consumer | esp_aec | esp_afe |
|----------|---------|---------|
| `esp_audio_stack` | yes | yes |

### Internal Pipeline

```text
single mic:
  esp_audio_stack task -> direct ESP-SR feed/fetch -> clean mono output

dual mic:
  esp_audio_stack task -> complete-frame feed ring
                       -> GMF manager feed/fetch + pipeline tasks
                       -> complete-frame output ring -> clean mono output
```

For one microphone, the wrapper owns the ESP-SR AFE instance directly and
executes its feed/fetch contract from the parent audio task. For two
microphones, Espressif's `esp_gmf_afe` element runs in GMF manager/pipeline
tasks. The wrapper keeps `process()` as the ESPHome-facing contract by
publishing complete feed frames into a NOSPLIT bridge ring and reading complete
processed frames without blocking. When process and feed shapes match, the GMF
path writes directly into an acquired ring slot; split-frame topologies retain
a staging scratch. Prepared rings/scratch remain allocated while the GMF path
is idle. Output writes are all-or-drop so fixed-size reads stay sample-aligned
after pressure.

## Complete Example

```yaml
external_components:
  - source:
      type: git
      url: https://github.com/n-IA-hane/esphome-audio-stack
      ref: main
    components: [esp_audio_stack, esp_afe]

esp_afe:
  id: afe_processor
  type: sr
  mode: low_cost
  mic_num: 2                  # 2 for dual-mic Speech Enhancement (default: 1)
  se_enabled: true            # Speech Enhancement (requires mic_num: 2, default: false)
  vad_enabled: true           # Voice activity detection (default: false)
  agc_enabled: false          # avoid AGC graph rebuilds during real-time audio

esp_audio_stack:
  id: audio_stack
  # ... I2S pins ...
  processor_id: afe_processor
  buffers_in_psram: true

# AFE switches
switch:
  - platform: esp_afe
    esp_afe_id: afe_processor
    aec:
      name: "Echo Cancellation"
    vad:
      name: "Voice Activity Detector"

# AFE diagnostic sensors
sensor:
  - platform: esp_afe
    esp_afe_id: afe_processor
    input_volume:
      name: "Input Volume"
    output_rms:
      name: "Output RMS"

# VAD binary sensor
binary_sensor:
  - platform: esp_afe
    esp_afe_id: afe_processor
    vad:
      name: "Voice Presence"

# Runtime mode switching
select:
  - platform: template
    id: afe_mode_select
    name: "AEC Mode"
    options:
      - sr_low_cost
      - sr_high_perf
    initial_option: sr_low_cost
    optimistic: false
    restore_value: false
    set_action:
      - esp_audio_stack.stop_and_wait: audio_stack
      - wait_until:
          condition:
            esp_audio_stack.is_idle: audio_stack
          timeout: 2s
      - esp_afe.set_mode:
          id: afe_processor
          mode: !lambda 'return x;'
      - wait_until:
          condition:
            lambda: 'return id(afe_processor).is_reconfigure_idle();'
          timeout: 20s
      - if:
          condition:
            lambda: 'return id(afe_processor).is_reconfigure_idle() && id(afe_processor).get_last_reconfigure_ok();'
          then:
            - esp_audio_stack.start: audio_stack
          else:
            - logger.log:
                level: ERROR
                format: "AFE mode switch failed; audio restart skipped"
      - lambda: 'id(afe_mode_select).publish_state(id(afe_processor).get_mode_name());'
```

## Memory Usage

ESP-SR owns the AFE graph, filter and internal-ring allocations. Their size and
internal/PSRAM placement vary with the resolved ESP-SR version, type/mode,
filter length, features and target. `memory_alloc_mode` influences that policy,
but does not make every private allocation user-placeable. The wrapper's
feed scratch and bridge rings have independent `*_in_psram` controls, and a
matching GMF frame shape can omit the feed scratch entirely.

Budget the final composite firmware from runtime free, minimum-free and largest
internal blocks plus PSRAM usage. Fixed totals copied from another board are
especially misleading across single-mic direct and dual-mic GMF graphs.

### ESP32-S3 IRAM/DRAM Profile

On ESP32-S3, IRAM and DRAM share the same 512 KB of SRAM. Every KB of code placed in IRAM reduces available DRAM heap by 1 KB. With `CONFIG_SPIRAM_FETCH_INSTRUCTIONS=y`, code runs from the PSRAM instruction cache, making IRAM placement unnecessary for many application functions.

PSRAM XIP is a useful starting point on maintained full-AFE profiles because it
can recover shared internal SRAM. Confirm flash/PSRAM mode support and measure
latency on the exact board:

```yaml
esp32:
  framework:
    type: esp-idf
    sdkconfig_options:
      CONFIG_SPIRAM_FETCH_INSTRUCTIONS: "y"
      CONFIG_SPIRAM_RODATA: "y"
```

Do not disable Wi-Fi/PHY IRAM paths as a default memory shortcut on full
audio devices. The saved RAM is small compared to the latency and throughput
risk on the same network path that carries TTS/media, API and VoIP traffic.

> **Tip**: Use ESPHome's native `debug` sensors (`free`, `block`, `min_free`,
> `fragmentation`, `psram`, `cpu_frequency`) for firmware-level diagnostics.
> Avoid permanent template heap sensors in standard YAMLs; use targeted runtime
> logs or `runtime_diag` only for short, low-level diagnostic sessions.

## Known Limitations

1. **Speech Enhancement replaces NS on dual-mic input**: With two microphone channels, `afe_config_check()` prioritizes SE/BSS over NS. SE/BSS is structural and is not a runtime toggle. Public dual-mic profiles keep AGC disabled and do not expose AGC controls because AGC changes require full AFE reinit.

2. **Runtime toggles**: AEC and VAD use the active direct/GMF control without rebuilding. NS/AGC and type/mode changes require a full AFE reinit.

3. **data_volume**: The AFE's built-in `data_volume` field is not used as a product signal in this ESPHome integration. Input/output RMS is computed locally instead.

4. **ESP-SR is closed source**: `memory_alloc_mode` influences its placement,
   but not every private allocation is controllable or observable. Measure the
   resolved graph instead of assuming a fixed unavoidable total.

5. **Single instance**: ESP-SR's FFT resources are a global singleton. Only one AFE (or AEC) instance can exist at a time.

## Troubleshooting

### AFE setup fails (NULL config)

Check logs for `afe_config_init failed`. This means esp-sr couldn't initialize with the requested input format/type/mode. Verify the `type`, `mode`, `mic_num`, and optional `input_format` combination.

### High internal RAM usage / esp-aes: Failed to allocate memory

If the final firmware leaves too little contiguous internal RAM, TLS/network
allocations or a high-performance AFE rebuild can fail. Do not normalize a low
headline free-heap value or infer corruption from that value alone; inspect
minimum free heap and the largest block around the actual failure. Useful
responses are:

1. Validate the maintained PSRAM-XIP profile for the board (see
   [ESP32-S3 IRAM/DRAM Profile](#esp32-s3-iramdram-profile)).
2. Set `memory_alloc_mode: more_psram`
3. Use a single-mic config or `esp_aec` if Speech Enhancement is not needed
4. Reduce `ringbuf_size` carefully; the single-mic direct path uses an effective
   minimum of 16 even when a smaller YAML value is requested
5. Consider using `esp_aec` instead if you don't need NS/AGC/VAD/SE

### Switch toggle has no effect

AEC and VAD are live through the selected direct/GMF control. NS and AGC
toggles require AFE reinit. Public dual-mic packages do not expose NS or AGC
toggles. If reinit is in progress, active AFE output is silenced instead of
exposing raw pre-AFE microphone audio.

### Voice presence always OFF

Ensure `vad_enabled: true` in the `esp_afe` config. VAD is disabled by default.

### Audio gap when toggling NS/AGC

Rebuilding the AFE causes a target-dependent gap that can extend to hundreds of
milliseconds. AEC can be toggled without rebuilding. Public dual-mic packages
avoid NS/AGC runtime toggles.

## Logging

The component logs under the tag `esp_afe`.

- `WARN` - GMF pipeline start failures, GMF manager toggle failures, AFE config failures, esp-sr allocation failures, mode-switch rebuild failure
- `INFO` - direct/GMF AFE lifecycle, live feature state and rebuild messages for runtime mode switches
- `DEBUG` - bridge feed/fetch instrumentation (only when `esp_audio_stack.telemetry: true`), per-stage enable/disable acks

To mute AFE chatter without losing project-wide DEBUG: `logger.logs.esp_afe: INFO`.

## License

The ESPHome wrapper code is MIT-licensed. ESP-SR, GMF and other fetched
Espressif dependencies retain their own licenses and product-use restrictions;
see the repository `THIRD_PARTY_NOTICES.md`.
