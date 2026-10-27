# Microphone delivery, analog tuning and PCM capture

Use the maintained device profile first. The options below add tools for a
specific board or experiment; none changes the existing profile defaults.
ESPHome 2026.10.0 or newer is required.

For wiring diagrams and YAML fragments covering each supported input layout,
start with [Microphone wiring and delivery, illustrated](../README.md#microphone-wiring-and-delivery-illustrated).

## 1. Choose how microphones reach the processor

Audio Stack owns slot extraction, rate conversion and microphone delivery.
AEC and AFE continue to use their existing input contracts.

| Configuration | Delivery | Processor configuration |
| --- | --- | --- |
| `mic_mix: passthrough` (default) | Selected microphone channels remain separate | AEC takes one; a dual-mic AFE uses `mic_num: 2` and SE/BSS |
| `mic_mix: average` | Arithmetic mean of two physical microphones, delivered as one channel | Mono AEC, mono AFE (`mic_num: 1`, no SE/BSS), or no processor |

For a TDM board with microphones in slots 0 and 2 and a reference in slot 1:

```yaml
esp_audio_stack:
  # Keep the rest of the board's bus, codec and processor settings.
  tdm_mic_slots: [0, 2]
  tdm_ref_slot: 1
  mic_mix: average
```

For two compatible microphones sharing a conventional stereo I2S input, select
one microphone as left and the other as right using their hardware channel
selection. They share BCLK, WS and the data line; check each microphone's
datasheet for its inactive-slot output behavior before connecting outputs.
No input codec or TDM emulation is required:

```yaml
esp_audio_stack:
  rx_slot_mode: stereo
  rx_mic_slots: [left, right]
  mic_mix: average
```

The same input layout with `mic_mix: passthrough` supplies two independent
channels to dual-microphone AFE/BSS. "Stereo" describes their transport here,
not a requirement to combine them or play them through two speakers.

| Physical input | Logical microphone selection | Available delivery |
| --- | --- | --- |
| One microphone | Its mono channel | Mono AEC, mono AFE or no processor |
| Stereo I2S, one data line | `rx_mic_slots: [left, right]` | Separate dual-AFE inputs or a mono average |
| TDM, with or without a codec | Two verified `tdm_mic_slots` | Separate dual-AFE inputs or a mono average |

The [codec-free I2S example](../examples/05-stereo-i2s-downmix.yaml) uses the same
downmix as the TDM example. Separate RX/TX buses also retain this microphone
delivery choice: `rx_bus` owns capture and `tx_bus` owns playback. This is not
two independent microphone receivers. A software playback reference remains
available for AEC when no codec supplies an analog reference.

Do not count the playback reference as a microphone. The current adapter accepts
at most two selected physical microphones; it does not provide arbitrary
three-or-more-microphone AFE. Two independent I2S data-input lines are also not
an existing stereo-input option. They require separate acquisition and sample
alignment, not a second downmix implementation or a configuration labelled TDM.
See the [ESP-IDF I2S bus and channel documentation](https://docs.espressif.com/projects/esp-idf/en/v5.5.6/esp32s3/api-reference/peripherals/i2s.html).

A reference channel is not a second microphone. Validation rejects a downmix
without two microphone slots, a stereo reference used as a microphone, and an
AFE still configured for two microphones. When switching from dual AFE, set
`mic_num: 1` and disable SE/BSS explicitly. The AEC and AFE implementations do
not perform or own this averaging.

The mean uses a 32-bit sum followed by division by two, with signed rounding
toward zero. Two full-scale inputs cannot overflow a 16-bit output. This is
not beamforming: opposite-polarity microphones can cancel each other. Check
wiring, polarity and the enclosure before deciding which delivery sounds best.

The operation runs in the existing conversion pass. It adds no task, wait or
separate DSP queue and does not force the I2S bus to 16 kHz. For example, the bus
may remain at 48 kHz while AEC receives 16 kHz. With `passthrough`, the downmix
implementation is excluded from compilation.

The converter also avoids deinterleave scratch when no sample-rate conversion
is necessary. The 32-to-16-bit conversion still uses Espressif's converter;
rate-changing paths retain the existing Audio Effects resampler. Digital gain
and DC filtering retain their existing order.

## 2. Adjust analog PGA without rebuilding

The optional `mic_gain` number on `platform: esp_audio_stack` sets post-processor
gain in dB. Analog PGA changes the input
codec before digital processing. A clipped ADC signal cannot be repaired by
reducing a digital gain later.

Mic Gain applies a constant factor, including the beginning of a quiet phrase.
It is independent of the AFE's optional automatic gain control. Positive gain
saturates at the PCM limits; keep enough headroom for nearby speech as well
as distant voices.

These optional numbers add analog controls:

```yaml
number:
  - platform: esp_audio_stack
    input_pga:
      name: Microphone PGA
    reference_pga:
      name: Reference PGA
```

`input_pga` requires a configured hardware input codec. `reference_pga` requires
ES7210 with an explicit physical `codec.input.ref_channel`. This is a codec
channel index, not a TDM memory-slot index. Use the board's verified mapping.
That reference must be enabled in `codec.input.mic_selected`. The microphone
control excludes that reference channel; the reference control
changes only that channel.

Both controls accept 0 to 37.5 dB. Optional `initial_value` sets a first-boot
default. A saved user setting takes precedence; without either, the existing
codec configuration supplies the default. Values survive codec close/reopen.

Codec drivers can quantize a requested gain to their supported hardware steps.
The number and configuration getter represent the request, not a register
readback. Use supported codec values and confirm the effective gain when exact
microphone/reference calibration matters.

Requests are applied by the existing audio owner. Changing a setting while
idle does not open I2S or start another worker. The number shows the requested
configuration; if a live driver write fails, it returns to the last accepted
value and logs the failure. A newer pending request is not overwritten by an
older result. Changing analog levels may require the echo canceller to adapt
again; no universal PGA, DAC boost or filter setting is imposed on all boards.

Omit these numbers to exclude their runtime control code. Ordinary fixed codec
gains keep working as before.

## 3. Record what each stage actually receives

Add `esp_audio_capture` to the same external component source as Audio Stack:

```yaml
external_components:
  - source: github://n-IA-hane/esphome-audio-stack@main
    components: [esp_audio_stack, esp_aec, esp_audio_capture]

# Give the existing stack and microphone explicit IDs if needed.
esp_audio_capture:
  id: audio_capture
  esp_audio_stack_id: audio_bus
  microphone_id: mic_main
  address: 192.0.2.10  # Replace with the receiving computer's IPv4 address.
  port: 19091
  duration: 15s
  buffer_size: 65536
  points: [raw_mic, reference, processor_input, processor_output, output]

button:
  - platform: template
    name: Start audio capture
    on_press:
      - esp_audio_capture.start: audio_capture
  - platform: template
    name: Stop audio capture
    on_press:
      - esp_audio_capture.stop: audio_capture
```

Use a local development checkout when testing changes that have not been
published. `microphone_id` must be the Audio Stack microphone attached to the
selected stack, not a second I2S reader. Capture is one optional component per
device, matching Audio Stack's single owner.

The receiver uses plain TCP on the development LAN. Start it on that computer,
then press the start button:

```sh
python tools/capture_audio.py captures/board-run-01 --port 19091
```

A complete worked configuration is
[04-dual-mic-downmix.yaml](../examples/04-dual-mic-downmix.yaml).
Remove its optional capture/number blocks if you only need microphone delivery.

| Point | What is recorded |
| --- | --- |
| `raw_mic` | The selected physical microphones, before downmix, DC filtering, digital gain and DSP; analog PGA is already applied |
| `reference` | The actual reference passed to the processor, after its normal routing/rate conversion; available when the processor runs |
| `processor_input` | Microphone data after extraction, rate conversion, optional downmix and input conditioning |
| `processor_output` | The processor's public output before `mic_gain`; bypass/unavailable processing retains its normal raw/silence behavior |
| `output` | Final mono microphone PCM, after `mic_gain`, as supplied to consumers |

Raw microphone PCM retains the bus sample rate and 16/32-bit container.
Processed surfaces are 16-bit at the configured output rate. Streams are saved
as separate WAV files with a JSON manifest. No hidden AFE stages such as
"AEC before NS" are invented: those vendor-internal taps are not exposed here.

Every record carries a stream sequence, sample position, monotonic timestamp,
session generation, rate, channels, width and cumulative queue-drop count.
Missing sample positions become zero-filled gaps in the WAV and remain listed
in the manifest. Duplicate, regressive, malformed or changed-format records
are rejected instead of being silently concatenated. Each surface has its own
timeline; asynchronous AFE output is not claimed to have sample-accurate
alignment with a raw-input record.

Capture duration is bounded to 100 ms through 120 seconds. The default is
15 seconds. A 64 KiB queue prefers PSRAM and is allocated only on start; larger
queues may be selected up to 256 KiB. A slow receiver causes diagnostic drops
or a bounded disconnect, not a wait in the realtime audio task. An individual
PCM record is capped at 16 KiB. The socket worker is temporary and returns its
queue and task resources at completion. When disabled, no buffer, worker or
socket exists. Omitting the component removes all capture hooks at compilation.

At the natural duration limit, the producer stops admitting data and the worker
finishes its current record before closing. Header and PCM share a one-second
transport budget. Mute, a new audio generation, a format change, parent failure,
explicit stop or transport failure can abort a record immediately. Capture owns
and releases only its own microphone-consumer reference. Stopping intentionally discards
unsent diagnostic data, so the trailing interval is not guaranteed to be
present. Receiver `complete` means that the received framing ended cleanly,
not proof that the requested duration or every sample was recorded. Inspect
sample positions and drops before using a capture as a benchmark.

## 4. Choose an assistant processing profile

Start with SR processing when the output feeds Micro Wake Word. FD can suppress
useful speech features as well as echo, particularly during playback. The
standalone defaults are AEC `sr_low_cost` and AFE `type: sr`, `mode: low_cost`.
Maintained Intercom FULL profiles select SR HIGH initially and offer SR LOW/HIGH;
VoIP-only profiles retain FD choices. Validate recognition and echo reduction
with the same external voice, playback level and enclosure before selecting a
mode. Separate microphone calibration from the recognizer's own input gain.

<a id="4-compare-on-the-same-board"></a>

## 5. Compare on the same board

Compare single-mic AEC, averaged dual-mic AEC and dual-mic AFE with the same bus,
playback content and application configuration. Record heap free/minimum,
largest internal block, PSRAM, task margins and audio continuity. Build RAM is
not runtime free heap, and a successful compile does not measure echo reduction
or wake-word accuracy. Keep the maintained profile until the alternative is
validated on the actual hardware.

The capture taps and microphone averaging use Audio Stack's existing multi-rate
I2S/codec owner and the existing AEC/AFE backends.
