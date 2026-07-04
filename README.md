# ESPHome Audio Stack

A full-duplex audio backend for ESPHome voice devices: I2S and codec ownership,
software echo cancellation, the complete Espressif AFE pipeline, and standard
ESPHome microphone and speaker surfaces on top.

This repository contains three ESPHome components:

| Component | Role |
|---|---|
| `esp_audio_stack` | Owns the physical audio path: I2S buses, TDM, hardware codecs, DMA, rate/bit-depth/channel conversion, playback buffering, the AEC reference and the audio task. Exposes ESPHome `microphone` and `speaker` platforms. |
| `esp_aec` | Standalone acoustic echo cancellation through Espressif ESP-SR AEC. Light on RAM and flash. |
| `esp_afe` | Full Espressif Audio Front End: AEC, noise suppression, VAD, AGC and dual-mic Speech Enhancement/BSS, with runtime switches and diagnostics. |

Everything above the stack stays normal ESPHome: Voice Assistant, Micro Wake
Word, `media_player`, mixer, resampler, VoIP components or your own C++
consumers. The stack does not replace the ESPHome audio ecosystem; it replaces
the hardware/audio ownership layer underneath it that native ESPHome does not
provide.

## 1. What This Is

ESPHome's native `i2s_audio` microphone and speaker work well when the two are
independent devices and no software echo cancellation is needed. Real voice
hardware is usually harder than that:

- one codec owns both the ADC and the DAC on the same I2S bus, so mic and
  speaker cannot be two independent components;
- software AEC needs a sample-aligned copy of what the speaker is playing, the
  playback reference;
- media playback, TTS, wake word, Voice Assistant and calls all share one
  speaker and one microphone;
- codecs speak 24/32-bit slots at 48 kHz while voice pipelines want 16 kHz mono
  `s16`;
- multi-mic boards deliver audio as TDM frames where microphone slots and the
  hardware echo-reference slot must be extracted at fixed positions.

`esp_audio_stack` centralizes that layer:

```text
I2S / codec / TDM / MEMS mic / I2S amp
        |
        v
esp_audio_stack
  - owns I2S and codec IO
  - converts rate, bit depth and channel layout
  - builds or captures the speaker reference for AEC/AFE
  - buffers speaker playback
        |
        v
optional processor: esp_aec or esp_afe
        |
        v
normal ESPHome microphone + speaker platforms
        |
        v
Voice Assistant, Micro Wake Word, media player, mixer, VoIP, custom logic
```

The design contract is deliberate: the stack solves the hardware and
signal-processing problem once, then disappears behind interfaces every ESPHome
component already understands. Consumers do not know or care whether the audio
came from a shared codec bus, a TDM frame, or two separate MEMS/amp buses.

## 2. What It Gives You

- **Full-duplex audio on one owner.** Simultaneous capture and playback on a
  shared codec bus, on split RX/TX buses, or on a TDM bus, driven by one pinned
  FreeRTOS task with an explicit runtime state machine: `idle`, `mic`,
  `speaker`, `duplex`.
- **Hardware codec control.** Built-in `esp_codec_dev` backends for ES7210,
  ES8311, ES8388, ES8374 and ES8389, configured from YAML with no custom C++.
- **Format conversion where it belongs.** The physical bus can run 48 kHz,
  32-bit, stereo or TDM while consumers receive 16 kHz, `s16`, mono. Rate
  conversion uses Espressif `esp_audio_effects`.
- **Every practical AEC reference topology.** Software reference from playback
  (`previous_frame` or an ADF Type2-style `ring_buffer`), stereo codec feedback,
  or a TDM hardware reference slot captured with the microphones.
- **Pluggable processing.** `processor_id` attaches `esp_aec` or `esp_afe`
  behind the microphone surface. They are mutually exclusive by validation.
- **The clean-mic contract.** When a processor is configured, the ESPHome
  microphone platform exposes only the post-processor stream. Wake word, Voice
  Assistant and call TX receive near-end speech after echo cancellation, never
  the raw feed.
- **On-demand lifecycle.** The audio task and I2S hardware start when the first
  consumer appears and stop when the last one leaves. Consumers are
  reference-counted.
- **Runtime control from Home Assistant.** Optional switch, number,
  binary_sensor and diagnostic sensor platforms expose AEC/AFE controls, mic
  gain, volume and TDM slot levels.
- **Strict YAML validation.** Invalid topologies are refused at compile time:
  impossible rate conversion, TDM slot collisions, unsupported SoCs, invalid
  core pinning, mutually exclusive processors and more.
- **Reproducible builds.** Espressif component-manager dependencies are pinned
  to tested versions and documented.

## 3. Scenarios It Covers

| Hardware / goal | Shape |
|---|---|
| Codec board, full-duplex audio, no echo cancellation | `esp_audio_stack` alone |
| INMP441 + MAX98357A prototype, simple duplex test | `esp_audio_stack` in dual-bus mode |
| Single-mic voice device that talks while it plays | `esp_audio_stack` + `esp_aec` |
| Voice Assistant + wake word + media + calls on one speaker | `esp_audio_stack` + `esp_aec` or `esp_afe` |
| Noisy room, variable speaker distance, VAD or AGC wanted | `esp_audio_stack` + `esp_afe` |
| Dual-mic board with Speech Enhancement/BSS | `esp_audio_stack` TDM + `esp_afe` |
| Wake word must keep working while TTS/media plays | `esp_aec` `sr_*` modes or `esp_afe` `type: sr` |
| Hardware already outputs echo-cancelled PCM, for example XMOS | ESPHome native audio may be enough; this stack is optional |

Supported SoCs, enforced by validation:

| Variant | I2S ports | Dual-bus mode | TDM |
|---|---:|---|---|
| ESP32-S3 | 2 | yes | yes |
| ESP32-P4 | 3 | yes | yes |
| ESP32 | 2 | yes | no |
| ESP32-C3 / C5 / C6 / C61 / H2 | 1 | no | yes |
| ESP32-S2 | 1 | no | no |

Realistic voice profiles with AEC/AFE at a 48 kHz bus rate target the S3 and P4
with PSRAM. Single-port chips can still run plain full-duplex audio on one
shared bus.

## 4. Core Concepts

**Bus rate vs output rate.** `sample_rate` is the physical I2S bus and speaker
rate. `output_sample_rate` is the microphone rate handed to consumers. A voice
device usually runs a 48 kHz bus with 16 kHz mic output. If
`output_sample_rate` is omitted, no conversion happens. When present, it must
divide `sample_rate` exactly and the ratio must not exceed 6.

**The reference.** AEC subtracts what the speaker played from what the
microphone heard. The topology decides where that playback reference comes
from: software (`aec_reference`), stereo codec feedback
(`use_stereo_aec_reference`), or a TDM slot (`use_tdm_reference`).

**The processor.** `esp_aec` and `esp_afe` implement one shared
`AudioProcessor` interface. `esp_audio_stack` feeds them mic frames plus the
reference and publishes their output as the microphone stream.

**Consumers and lifecycle.** Nothing runs while nothing listens. The audio task,
DMA channels and codec paths spin up when the first microphone listener or
speaker stream arrives and wind down after the last one leaves.

**The real-time boundary.** The audio task runs at high priority, default 19,
pinned to core 0 by default. Any C++ callback invoked from it must not block,
allocate or do I/O.

## 5. Installation

Pull only the components your YAML needs.

Full-duplex audio only:

```yaml
external_components:
  - source: github://n-IA-hane/esphome-audio-stack@main
    components: [esp_audio_stack]
```

With standalone AEC:

```yaml
external_components:
  - source: github://n-IA-hane/esphome-audio-stack@main
    components: [esp_audio_stack, esp_aec]
```

With full AFE:

```yaml
external_components:
  - source: github://n-IA-hane/esphome-audio-stack@main
    components: [esp_audio_stack, esp_afe]
```

Requirements:

- ESP-IDF framework. Arduino is not supported.
- PSRAM. The component schema requires the ESPHome `psram:` component so memory-heavy audio paths fail at YAML validation time instead of at runtime.
- An `i2c:` bus when a hardware codec is configured.

Espressif dependencies are resolved automatically by the IDF Component Manager.
Nothing is vendored.

## 6. Hardware Topologies

### 6.1 Single-Bus Codec

One codec handles both directions on a shared I2S bus. This is the common shape
for compact voice boards: one I2C-controlled codec, one I2S port, mic ADC,
speaker DAC and optional hardware echo feedback.

```yaml
i2c:
  sda: GPIO47
  scl: GPIO48
  frequency: 400kHz

esp_audio_stack:
  id: audio_stack
  sample_rate: 48000
  output_sample_rate: 16000
  bits_per_sample: 32
  slot_bit_width: 32

  i2s_mclk_pin: GPIO5
  i2s_bclk_pin: GPIO6
  i2s_lrclk_pin: GPIO7
  i2s_din_pin: GPIO4
  i2s_dout_pin: GPIO8

  codec:
    input:
      type: es8311
      address: 0x18
    output:
      type: es8311
      address: 0x18
```

If the codec supports digital DAC feedback, prefer the stereo reference over
the software one.

### 6.2 Dual-Bus, No Codec

One I2S peripheral reads the microphone, another drives the speaker amplifier.
Typical parts: INMP441 or ICS MEMS mic plus MAX98357A-class I2S amp. Requires an
SoC with at least two I2S ports.

```yaml
esp_audio_stack:
  id: audio_stack
  sample_rate: 48000
  output_sample_rate: 16000
  bits_per_sample: 32
  slot_bit_width: 32

  rx_bus:
    i2s_num: 0
    i2s_bclk_pin: GPIO12
    i2s_lrclk_pin: GPIO13
    i2s_din_pin: GPIO11

  tx_bus:
    i2s_num: 1
    i2s_bclk_pin: GPIO9
    i2s_lrclk_pin: GPIO10
    i2s_dout_pin: GPIO14

  rx_slot_mode: stereo
  mic_channel: right

  # Lightweight software reference. Use ring_buffer when the enclosure needs
  # a delay-tunable AEC reference.
  aec_reference: previous_frame
```

Rules enforced at validation: `rx_bus` and `tx_bus` must be configured
together, must use different `i2s_num` values, and top-level I2S data pins must
not be set alongside them. Dual-bus mode does not support TDM. There is no
hardware feedback channel in this topology, so AEC uses the software reference.

### 6.3 TDM Codec With Hardware Reference

Multi-slot TDM input through an ADC codec, with the speaker DAC feedback
occupying one slot. This is the strongest software-AEC topology available: the
reference is captured by hardware in the same TDM frame as the microphones.

Single mic plus hardware reference:

```yaml
esp_audio_stack:
  id: audio_stack
  processor_id: afe_processor
  sample_rate: 48000
  output_sample_rate: 16000
  bits_per_sample: 32
  slot_bit_width: 32

  i2s_mclk_pin: GPIO5
  i2s_bclk_pin: GPIO6
  i2s_lrclk_pin: GPIO7
  i2s_din_pin: GPIO4
  i2s_dout_pin: GPIO8

  use_tdm_reference: true
  tdm_total_slots: 4
  tdm_mic_slot: 0
  tdm_ref_slot: 2
  tdm_tx_slot: 0

  codec:
    input:
      type: es7210
      address: 0x40
    output:
      type: es8311
      address: 0x18
```

Dual mic plus hardware reference for Speech Enhancement/BSS:

```yaml
esp_afe:
  id: afe_processor
  type: sr
  mode: low_cost
  mic_num: 2
  se_enabled: true
  input_format: mmr
  aec_enabled: true
  ns_enabled: false
  agc_enabled: false

esp_audio_stack:
  id: audio_stack
  processor_id: afe_processor
  sample_rate: 48000
  output_sample_rate: 16000

  tdm_total_slots: 4
  tdm_mic_slots: [0, 2]
  use_tdm_reference: true
  tdm_ref_slot: 1
  tdm_tx_slot: 0
```

Slot numbers are zero-based physical TDM slots. The reference slot must differ
from every mic slot, and `tdm_total_slots` must exceed the highest index in
use. Use the schematic, and when in doubt use per-slot level sensors to find the
slot that moves during playback.

## 7. Echo Cancellation Reference Topologies

### 7.1 Software Reference

When no hardware feedback exists, the stack derives the reference from the
speaker playback path.

| Mode | What it does | Cost | Use it when |
|---|---|---|---|
| `ring_buffer` | Stores converted speaker TX frames in an Espressif ADF Type2-style ring buffer. Capacity is `aec_reference_buffer_ms`, default 80 ms, range 32 to 500. | More RAM and one ring read/write per frame. | No-codec enclosures where acoustic delay needs tuning or `previous_frame` leaves echo. |
| `previous_frame` | Converts the latest speaker TX frame to the processor rate and reuses it as the next AEC reference frame. | Lowest RAM and smallest compiled path. No delay tuning. | Battery devices, simple prototypes, or tested layouts where the speaker/mic path is already close enough. |

This mode is ignored automatically when a stereo or TDM reference is configured.

### 7.2 Stereo Codec Feedback

ES8311-class codecs can route DAC output back as the second ADC channel. The
stack reads stereo input where one channel is the user's microphone and the
other is the playback reference.

```yaml
esp_audio_stack:
  id: audio_stack
  processor_id: aec_processor
  num_channels: 2
  use_stereo_aec_reference: true
  reference_channel: right

  codec:
    input:
      type: es8311
      address: 0x18
      no_dac_ref: false
    output:
      type: es8311
      address: 0x18
      no_dac_ref: false
```

This is the recommended AEC topology on ES8311 boards.

### 7.3 TDM Hardware Reference

TDM hardware reference is described in section 6.3. It is sample-aligned by
hardware, supports one or two microphone slots, and is the required shape for
dual-mic BSS with a real reference.

`use_stereo_aec_reference` and `use_tdm_reference` are mutually exclusive. A
board has one hardware reference topology at a time, and the validator enforces
it.

One common confusion: the AFE `R` channel does not have to be a physical slot.
The processor is always fed a reference buffer; the topology only decides
whether that buffer comes from hardware or from the playback stream.

## 8. Processors

### 8.1 `esp_aec`: Standalone Echo Cancellation

`esp_aec` wraps ESP-SR AEC as a minimal `AudioProcessor`. It is fixed at 16 kHz.

```yaml
esp_aec:
  id: aec_processor
  sample_rate: 16000
  mode: sr_low_cost
  filter_length: 4

esp_audio_stack:
  id: audio_stack
  processor_id: aec_processor
```

| Mode | Use case |
|---|---|
| `sr_low_cost` | Best starting point for Voice Assistant and Micro Wake Word. |
| `sr_high_perf` | Stronger SR AEC, with more internal memory pressure. |
| `fd_low_cost` / `fd_high_perf` | Full-duplex modes with NLP for codec targets where residual speaker echo is audible. |
| `voip_low_cost` / `voip_high_perf` | VoIP-oriented suppression. Can hurt wake-word detection. |

The mode can be switched at runtime with `esp_aec.set_mode`.

### 8.2 `esp_afe`: Full Audio Front End

`esp_afe` wraps the Espressif AFE/GMF pipeline: AEC, noise suppression, VAD, AGC
and dual-mic Speech Enhancement/BSS.

```yaml
esp_afe:
  id: afe_processor
  type: sr
  mode: low_cost
  mic_num: 1
  aec_enabled: true
  ns_enabled: true
  vad_enabled: false
  agc_enabled: true

esp_audio_stack:
  id: audio_stack
  processor_id: afe_processor
```

| Type | Use case |
|---|---|
| `sr` | Speech recognition profile. Right default for assistant devices. |
| `vc` | Voice communication profile with stronger residual suppression. |
| `fd` | Full-duplex pipeline with NLP baked in, for two-way speech. |

Dual-mic Speech Enhancement requires `mic_num: 2`, `se_enabled: true`, two mic
slots on a TDM board, and an `input_format` matching the ESP-SR channel order of
your board port.

`input_format` letters:

| Letter | Meaning |
|---|---|
| `M` | Microphone channel |
| `N` | Unknown/unused channel, usually zero or ignored by the pipeline |
| `R` | Playback reference channel for AEC |

Supported values are `auto`, `mr`, `mnr`, `mmr`, `mmnr`. Leave it on `auto`
unless porting a known board topology.

`esp_aec` and `esp_afe` are mutually exclusive in one firmware.

## 9. Microphone and Speaker Surfaces

### 9.1 Microphone

```yaml
microphone:
  - platform: esp_audio_stack
    id: clean_mic
    esp_audio_stack_id: audio_stack
```

The platform publishes mono `s16` audio at `output_sample_rate`. When
`processor_id` is configured, this is the post-processor stream. Feed it to
normal ESPHome consumers:

```yaml
micro_wake_word:
  microphone: clean_mic
  models:
    - model: okay_nabu

voice_assistant:
  microphone: clean_mic
  media_player: speaker_media_player
  micro_wake_word: mww
```

During TTS or media playback, wake word keeps working because the speaker signal
has been subtracted. During a call, the assistant reacts to the person in the
room, not to the remote caller's voice coming out of the speaker.

### 9.2 Speaker

```yaml
speaker:
  - platform: esp_audio_stack
    id: speaker_out
    esp_audio_stack_id: audio_stack
    sample_rate: 48000
    bits_per_sample: 16
    buffer_duration: 500ms
```

The speaker accepts 16-bit PCM, one or two channels, 8 to 48 kHz, and plays at
the bus rate. Combine it with ESPHome resampler and mixer speakers upstream
when multiple sources at multiple rates share the output.

## 10. Lifecycle, Automations and Runtime Entities

Runtime state is one of `idle`, `mic`, `speaker`, `duplex`.

| Trigger | Fires when |
|---|---|
| `on_start` / `on_idle` | The stack leaves / returns to idle. |
| `on_state` | Any state change. The new state is passed as a string. |
| `on_mic_start` / `on_mic_idle` | Capture starts / stops. |
| `on_speaker_start` / `on_speaker_idle` | Playback starts / stops. |
| `on_amplifier_required` / `on_amplifier_idle` | Speaker-path aliases for GPIO amp control. |

```yaml
esp_audio_stack:
  id: audio_stack
  on_amplifier_required:
    then:
      - output.turn_on: speaker_enable
  on_amplifier_idle:
    then:
      - output.turn_off: speaker_enable
```

Actions and conditions:

| Item | Meaning |
|---|---|
| `esp_audio_stack.start` | Start the audio path explicitly. |
| `esp_audio_stack.stop` | Request a stop. |
| `esp_audio_stack.stop_and_wait` | Stop and wait for teardown. |
| `esp_audio_stack.is_idle` | Condition true when the stack is idle. |

Runtime entities:

```yaml
switch:
  - platform: esp_audio_stack
    esp_audio_stack_id: audio_stack
    aec:
      name: Echo Cancellation
      restore_mode: RESTORE_DEFAULT_ON

number:
  - platform: esp_audio_stack
    esp_audio_stack_id: audio_stack
    master_volume:
      name: Master Volume
      speaker_id: speaker_out
    mic_gain:
      name: Mic Gain
```

AFE switches:

```yaml
switch:
  - platform: esp_afe
    esp_afe_id: afe_processor
    aec:
      name: Echo Cancellation
    ns:
      name: Noise Suppression
    vad:
      name: Voice Activity Detector
    agc:
      name: Auto Gain Control
```

TDM slot sensors exist to answer the common bring-up question empirically: play
music, watch which slot moves, and you have found your reference slot; speak,
and you have found the mic slots.

## 11. Configuration Reference

All values are verified against the component schema.

### Core Audio

| Option | Default | Range / values | Meaning |
|---|---:|---|---|
| `sample_rate` | `16000` | 8000 to 48000 | Physical I2S bus and speaker rate. |
| `output_sample_rate` | `sample_rate` | 8000 to 48000 | Microphone rate exposed to consumers. Must divide `sample_rate`, ratio at most 6. |
| `bits_per_sample` | `16` | 16, 24, 32 | Sample container on the bus. |
| `slot_bit_width` | `auto` | auto, 16, 24, 32 | Physical slot width. |
| `num_channels` | `1` | 1, 2 | RX channel count on standard I2S. |
| `speaker_channels` | `1` | 1, 2 | Playback channels. Two channels require standard I2S and `num_channels: 2`. |
| `mic_channel` | `left` | left, right | Which stereo slot carries the mic when RX is stereo. |
| `rx_slot_mode` | `mono` | mono, stereo | Read one or both stereo slots on RX. |
| `tx_channel` | `left` | left, right | TX slot placement in mono-on-stereo layouts. |
| `correct_dc_offset` | `false` | bool | Remove DC bias from capture. |
| `input_gain` | `1.0` | 0.01 to 32.0 | Digital gain before the processor. |
| `master_volume_min_db` | codec-dependent | -96.0 to 0.0 | Bottom of the volume curve. |

### I2S Bus

| Option | Default | Range / values | Meaning |
|---|---:|---|---|
| `i2s_num` | `0` | SoC port index | I2S peripheral for single-bus mode. |
| `i2s_lrclk_pin`, `i2s_bclk_pin` | required | GPIO | Bus clocks. |
| `i2s_mclk_pin` | `-1` | GPIO or -1 | Master clock. |
| `i2s_din_pin`, `i2s_dout_pin` | `-1` | GPIO | Data in / data out. |
| `i2s_mode` | `primary` | primary, secondary | Clock master or slave. |
| `i2s_comm_fmt` | `philips` | philips, msb, pcm_short, pcm_long | Frame format. PCM short/long are TDM-only. |
| `mclk_multiple` | `256` | 128, 256, 384, 512 | MCLK to sample-rate ratio. |
| `use_apll` | `false` | bool | APLL clock source, only on supported variants. |
| `rx_bus` / `tx_bus` | none | object | Dual-bus mode with separate I2S controllers. |
| `dma_desc_num` | `6` | 2 to 16 | DMA descriptor count. |
| `dma_frame_num` | auto | 64 to 4092 | Frames per descriptor. |

### Processor and Reference

| Option | Default | Range / values | Meaning |
|---|---:|---|---|
| `processor_id` | none | id | Attach `esp_aec` or `esp_afe`. |
| `aec_reference` | `ring_buffer` | ring_buffer, previous_frame | Software reference mode. Ignored when stereo/TDM reference is active. |
| `aec_reference_buffer_ms` | `80` | 32 to 500 | Ring capacity for `ring_buffer`. |
| `use_stereo_aec_reference` | `false` | bool | Stereo codec DAC feedback as reference. |
| `reference_channel` | `left` | left, right | Which stereo channel carries the feedback. |
| `use_tdm_reference` | `false` | bool | A TDM input slot carries the hardware reference. |
| `tdm_total_slots` | `4` | 2 to 8 | Slots in the physical TDM frame. |
| `tdm_mic_slot` | `0` | 0 to 7 | Single mic slot. |
| `tdm_mic_slots` | none | list of 1 or 2 | Multi-mic slot list. Enables TDM bus. |
| `tdm_ref_slot` | `1` | 0 to 7 | Reference slot. Must differ from mic slots. |
| `tdm_tx_slot` | `0` | 0 to 7 | Playback slot. |

### Codec Block

| Option | Values | Notes |
|---|---|---|
| `input.type` | es7210, es8311, es8388, es8374, es8389 | ADC side. |
| `output.type` | es8311, es8388, es8374, es8389 | DAC side. |
| `address` | I2C address | Defaults: ES7210 `0x40`, ES8311 `0x18`, others `0x20`. |
| `gain_db` | 0.0 to 37.5 | Analog mic gain. |
| `mic_selected` | bitmask | ES7210 ADC channel mask, default `0x0F`. |
| `ref_channel` / `ref_gain_db` | channel / dB | ES7210 reference routing and gain. |
| `use_mclk` | bool | ES8311/ES8389 clocking mode. |
| `no_dac_ref` | bool | Set ES8311 input side to `false` for stereo DAC feedback. |

### Task, Memory and Diagnostics

| Option | Default | Range / values | Meaning |
|---|---:|---|---|
| `task_priority` | `19` | 1 to 24 | Audio task priority. |
| `task_core` | `0` | -1 to 1 | Core pinning. Core 1 is rejected on single-core SoCs. |
| `task_stack_size` | `8192` | 4096 to 32768 | Audio task stack. |
| `buffers_in_psram` | `false` | bool | Move non-DMA audio buffers to PSRAM. |
| `audio_task_stack_in_psram` | `false` | bool | Move the audio task stack to PSRAM through ESPHome's PSRAM task-stack helper. Requires the `psram` component. |
| `aec_ref_ring_in_psram` | `false` | bool | Put the Type2 reference ring in PSRAM. |
| `telemetry` | `false` | bool | Per-stage cycle counting and diagnostics. Debug only. |
| `telemetry_log_interval_frames` | `128` | 1 to 8192 | Telemetry log cadence. |
| `audio_effects.rate_cvt_complexity` | `3` | 1 to 3 | Rate converter quality/CPU trade-off. |
| `audio_effects.rate_cvt_perf_type` | `speed` | speed, memory | Rate converter optimization target. |

Full per-component references:

- [`esp_audio_stack`](esphome/components/esp_audio_stack/README.md)
- [`esp_aec`](esphome/components/esp_aec/README.md)
- [`esp_afe`](esphome/components/esp_afe/README.md)

## 12. What The Validator Refuses

Audio bring-up failures are miserable to debug at runtime, so this component
front-loads many of them into YAML compilation. It rejects:

- `sample_rate` not divisible by `output_sample_rate`, or ratio above 6;
- TDM mic/reference slot collisions, duplicate mic slots, or too few total
  slots;
- `use_tdm_reference` together with `use_stereo_aec_reference`;
- `speaker_channels: 2` on TDM or without `num_channels: 2`;
- `pcm_short` / `pcm_long` outside TDM mode;
- `rx_bus` without `tx_bus`, both on the same `i2s_num`, top-level bus pins
  mixed with dual-bus mode, or dual-bus on a single-port SoC;
- I2S port numbers beyond the target SoC;
- TDM options on SoCs without TDM support;
- `task_core: 1` on single-core variants;
- `use_apll` on variants without APLL;
- `esp_aec` and `esp_afe` in the same firmware;
- `esp_afe` feed and fetch tasks pinned to the same core;
- `processor_id` configured on both `esp_audio_stack` and `voip_stack`.

If your YAML compiles, the topology is at least physically coherent for your
chip.

## 13. Performance And Memory Notes

- ESP Audio Stack profiles require PSRAM on supported ESP32 targets.
- DMA descriptors and I2S buffers always live in internal RAM.
- `esp_aec` is the lighter path.
- `esp_afe` costs more RAM and flash and gives the full speech front end plus
  diagnostics.
- PSRAM placement options trade internal-RAM headroom for latency. Enable them
  individually only when memory pressure is real.
- Keep `logger.level: INFO` on release firmware. `telemetry` and DEBUG logging
  are diagnostic tools; per-frame logging on the audio core can itself cause
  audio glitches.

## 14. Examples

| File | Shows |
|---|---|
| [`examples/01-esp-audio-stack-only.yaml`](examples/01-esp-audio-stack-only.yaml) | Codec-backed full-duplex mic/speaker, no processing. |
| [`examples/02-esp-audio-stack-aec.yaml`](examples/02-esp-audio-stack-aec.yaml) | The same base with `esp_aec`. |
| [`examples/03-esp-audio-stack-afe.yaml`](examples/03-esp-audio-stack-afe.yaml) | The same base with `esp_afe`. |

The examples are intentionally minimal. Product YAMLs layer mixer, resampler,
media player, wake word, Voice Assistant, display and call logic on top.

## 15. Provenance, Dependencies And License

This repository was extracted from the maintained
[`n-IA-hane/esphome-intercom`](https://github.com/n-IA-hane/esphome-intercom)
codebase, where this stack is the audio backend of a full SIP intercom platform
and is exercised on real ES8311, ES7210/ES8311, ESP32-S3 and ESP32-P4 hardware.
`SOURCE.md` records the source commit of each snapshot.

Espressif dependencies and their pins:

- `esp_codec_dev` `1.5.10` for codec control;
- `esp_audio_effects` `1.3.0~1` for rate, bit-depth and layout conversion;
- `esp-sr` and GMF AFE components for the `esp_aec` / `esp_afe` processors.

Pins are temporary, documented and removed after board testing.

This repository is MIT-licensed. Espressif dependencies keep their own licenses
and hardware restrictions; no Espressif source or binaries are vendored here.
