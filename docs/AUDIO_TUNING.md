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

## 2. Give each gain a specific job

A loud signal before AEC and a quiet voice at the far end of a call can coexist.
Measure the signal at both boundaries. Turning up the input also changes the
acoustic echo path; a call-only level adjustment should affect only the call.

| Control | Unit | Scope |
| --- | --- | --- |
| `number.input_pga` | dB | Input codec before AFE. Analog PGA on ES7210; digital ADC scale on ES8311. |
| `number.reference_pga` | dB | Independent physical ES7210 reference channel; never the microphones. |
| `esp_audio_stack.input_gain` | Linear factor | Microphone PCM before the processor. |
| `esp_afe.afe_linear_gain` | Linear factor | Vendor AFE output path; may also affect VAD and channel selection. |
| `number.mic_gain` | dB | Shared processor output delivered to all microphone consumers. |
| VoIP Stack `number.mic_gain` | dB | Call TX only, after microphone processing. Does not change MWW or Assist. |
| Master volume | Percent | Playback, including its actual configured DAC curve. Percent is not dB. |

**0 dB means a factor of 1**, not mute. Positive digital gain can saturate PCM;
lowering another stage cannot recover lost samples. `raw_mic` already contains
the input codec's analog and digital scaling. Its name does not mean unamplified
ADC data. Reference amplitude need not equal microphone amplitude: verify its
routing, timing and headroom instead of forcing equal RMS.

For an ES7210 with a separately wired reference:

```yaml
number:
  - platform: esp_audio_stack
    input_pga:
      name: Microphone Gain (pre-processing)
    reference_pga:
      name: AEC Reference Gain
```

`reference_pga` requires an explicit physical `codec.input.ref_channel` enabled
in `mic_selected`. The index identifies a codec channel, not a DMA/TDM slot.
Microphone writes exclude that channel. For ES8311 use only `input_pga`: the
internal playback feedback has no independently adjustable reference gain in
this driver. Its pre-AFE control adjusts digital ADC scale, not the separate
analog PGA initialized by the codec driver.

ES7210 controls accept 0..37.5 dB and normalize through the resolved vendor
mapping. In esp_codec_dev 2.0.0-beta5, requested20 dB becomes18 and20.5 becomes21.
The ES8311 control exposes0..36 dB in6 dB steps, rounding down. Omitting
`initial_value` preserves the saved setting or codec default. Existing saved
requests normalize to the same physical gain the driver previously applied.
Do not copy calibration values from another board or enclosure.

The existing audio owner applies requests. A closed codec accepts configuration
without claiming a hardware write. On supported codecs the owner reads physical
registers; matching microphone gains and the separate reference are reported
independently. An unreadable or mixed multichannel result is unknown, rather
than an invented previous value. A failed multichannel write may have changed
some registers. Values are saved only when valid and reproducible; preference
saving follows ESPHome's deferred flash behavior.

Omitting these controls excludes their control code. Optional
`hardware_gain_diagnostics: true` on the number platform compiles a one-shot
register dump requested with `id(audio_stack).request_hardware_gain_diagnostics()`
(use your actual stack ID). It does not create another worker, open an idle
codec or reset AFE. The normal controls need no diagnostic logging.

Native AFE output gain is not AEC strength. For example:

```yaml
esp_afe:
  afe_linear_gain: 1.0
```

In the verified ESP-SR2.6.0 dual-microphone path it acts after AEC/BSS but before
VAD and final output selection. A shared post-gain, an internal AFE gain and a
call TX gain therefore have different effects. Keep one owner for each job;
avoid cascading boosts to compensate for an unknown attenuation.

WakeNet-derived AGC requires its model state. Espressif also exposes standalone
`esp_agc_*` processing without WakeNet, operating on10 ms blocks. Dual-mic
post-AGC support is optional here; it does not repair upstream clipping or
change AEC retroactively. Preserve the recognition model's frontend and assess
speech, residual playback, latency and resource cost before enabling it.

A volume-dependent wake-word limit does not alone prove an AFE software defect.
Check ADC/reference/mixer saturation and speaker or amplifier distortion.
Feedback represents its actual tap point and may exclude distortion generated
later by the amplifier. Record a qualified operating range per board when no
further correction is demonstrated; never silently clamp the user's Master.

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

At the natural duration limit, the producer stops admitting data. The worker
waits for admitted writers and drains their queued records, with transport
bounded to one second beyond that limit. Header and PCM also share the existing
per-record budget. Mute, a new audio generation, a format change, parent failure,
explicit stop or transport failure can abort a record immediately. Capture owns
and releases only its own microphone-consumer reference. An abort or exhausted
drain budget can discard unsent diagnostic data, so the trailing interval is
not guaranteed to be present. Receiver `complete` means that the received
framing ended cleanly, not proof that the requested duration or every sample
was recorded. Inspect sample positions and drops before using a capture as a
benchmark.

### Optional native AFE output capture

The normal configuration omits `esp_audio_capture`, keeps `telemetry: false`
and leaves `enable_test_input: false`. The AFE diagnostic method remains
callable and reports that its detailed snapshot was omitted. Enabling telemetry
or PCM capture includes that snapshot; detailed per-frame counters additionally
require the existing debug logging gate.

For a paired diagnostic recording, extend the existing Audio Stack source with
`esp_audio_capture` and use the IDs of the already configured stack, microphone
and AFE. For example:

```yaml
esp_audio_capture:
  id: diagnostic_capture
  esp_audio_stack_id: audio_bus
  microphone_id: mic_afe
  afe_id: afe_processor
  address: 192.0.2.10  # Replace with the receiver's IPv4 address.
  duration: 24s
  buffer_size: 262144
  task_stack_in_psram: true
  boundary_probes: false
  points:
    - raw_mic
    - reference
    - processor_input
    - processor_output
    - output
    - afe_selected
    - afe_separated
```

`afe_separated` records the first two interleaved vendor output planes in one
stereo WAV; `afe_selected` records the vendor's selected mono PCM. These are
DSP outputs, not physical microphone identities. They are available for vendor
results containing three output planes. Other layouts are not reinterpreted
as three planes. The observer borrows the vendor pointers only during the
native fetch callback and copies into the existing capture queue after normal
selected-output admission. It does not alter selection, gain or samples.

`boundary_probes: true` additionally records native AFE feed and samples
consumed by the existing MWW frontend. It requires an existing MWW component,
uses linker wrappers only in that diagnostic build, and preserves the native
calls and return values. It adds no model or inference observer. Session,
overflow and payload checksums are evidence to validate, not acoustic proof.
Keep it false for ordinary capture and normal firmware.

The optional `esp_audio_stack.enable_test_input` defaults to false. It exposes
a synchronous, borrowed-buffer injection callback for a separately configured
test source; it does not create a source, server or player. Leave it false
for physical microphone recordings. These gates exclude their pointers,
callbacks and boundary registries when disabled. Omitting capture excludes
its worker, socket and queue. An exhausted transport deadline never permits
freeing the queue while admitted writers still use it.

### Amplifier readiness before playback

An `esp_audio_stack` speaker can set `amplifier_settle_time` to a duration or
a lambda returning milliseconds. Omitted or constant `0ms` excludes the gate.
There is no global 250ms default. Board packages choose a value from their
power policy; for a board whose idle-powerdown timer means the amplifier is
still on, the existing pattern is:

```yaml
speaker:
  - platform: esp_audio_stack
    id: hw_speaker
    amplifier_settle_time: !lambda |-
      return id(audio_stack_speaker_idle_powerdown).is_running() ? 0 : 250;
```

The existing speaker owner evaluates the policy before requesting hardware,
starts the interval after hardware readiness, and applies backpressure until
RUNNING. Caller-owned PCM remains pending. Cancellation and a warm restart
cannot erase an unfinished cold interval. This covers startup, not a board's
separate mute/unmute transition while a stream is already running.

## 4. Choose an assistant processing profile

Start with SR processing when the output feeds Micro Wake Word. FD can suppress
useful speech features as well as echo, particularly during playback. The
standalone defaults are AEC `sr_low_cost` and AFE `type: sr`, `mode: low_cost`.
Maintained Intercom FULL profiles select SR HIGH initially and offer SR LOW/HIGH;
VoIP-only profiles retain FD choices. Validate recognition and echo reduction
with the same external voice, playback level and enclosure before selecting a
mode. Separate microphone calibration from the recognizer's own input gain.

### Playback operating range depends on the board

User observations put the practical ceiling near 60% Master on the Waveshare
S3 Touch LCD 1.85C Box V2. Spotpear Ball V2 was reported to recognize reliably up
to 85% in one workload, but a later report also describes recognition failing
above 60%. Do not treat 85% as a general guarantee: the usable range depends on
playback content, gains, concurrent work and enclosure. These are operating
observations, not certified limits or a controlled ten-trial result. They are
not profile defaults and must not automatically clamp a user's volume or be
copied to another board.

Three far-only Spotpear captures at 60%, 85% and 100% found no digital PCM clipping
in `raw_mic`, `reference` or `output`. The 100% exposure was brief: Master0 was
observed about 4.16s after raising it, versus about 10s for the other two cases.
After aligning the same spoken phrase, the reference stayed effectively
unchanged while microphone and post-AFE levels increased. ES8311's digital
feedback precedes DAC attenuation; a reference that does not follow Master is
not automatically a defect. Do not force reference scaling to track Master.
AGC and NS were active, so output/raw level ratios are not AEC ERLE. There was
no near-end human speech in these captures: they do not establish recognition accuracy
or explain the reported high-volume limit by themselves. Compare each board
with real near-end speech, its actual reference path and the intended playback
level before changing its operating range.

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
