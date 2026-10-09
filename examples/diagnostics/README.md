# ES8311 read-only diagnostics

Use this optional package when I2C detects an ES8311 but microphone capture or
speaker playback is silent. It adds a manually triggered button using ESPHome's
existing I2C bus. There is no periodic polling, persistent diagnostic state or
change to Audio Stack's realtime tasks.

Download `es8311-registers.yaml` beside your device YAML and merge:

```yaml
substitutions:
  es8311_diagnostic_i2c_id: i2c_1
  es8311_diagnostic_address: "0x18"

packages:
  es8311_probe: !include es8311-registers.yaml
```

Use the ID of your existing codec I2C bus and the 7-bit scan address (normally
0x18 or 0x19). Keep your board wiring and codec configuration unchanged. Compile
and upload, open the device logs at INFO or DEBUG, then press **ES8311 register
diagnostics**. Remove the package after the investigation.

## Capture the operating state

Collect one complete `BEGIN` to `END` block for each state:

1. Idle, to establish the powered-down baseline.
2. Microphone capture active, while speaking and with no speaker playback.
3. Playback active, during a known audio clip lasting several seconds.

Save the corresponding `esp_audio_stack.dump_diagnostics` output, firmware
version and full relevant YAML alongside each block. The register probe does
not start capture or playback. Read failures are explicitly marked; they must
not be interpreted as register values of zero. The probe stops at the first
failed read to avoid repeatedly blocking on an unavailable bus. These are
sequential reads, not
an atomic snapshot across a concurrent start/stop transition.

## What to compare

| Registers | Check |
| --- | --- |
| FD, FE, FF | Chip identity and revision; verify against the ES8311 datasheet |
| 00, 01-08 | Reset state, selected clock source and divider configuration |
| 09, 0A | DAC/ADC serial format, word length and serial mute/channel controls |
| 0D, 0E, 12 | Power configuration while the requested direction is active |
| 14 | Analog/digital microphone selection and microphone preamplifier |
| 16, 17 | ADC gain and digital level |
| 31, 32 | DAC mute/control and playback level |
| 44, 45 | Reference/output routing configuration |

Compare registers against the actual driver version and requested format.
For example, the codec's 16-bit and 32-bit serial words have different settings;
a working 16-bit example is not a register table to paste over a 32-bit device.
Power-down values after a call are not evidence that the active call was muted.

The native ESPHome ES8311 microphone-selection report
[#17695](https://github.com/esphome/esphome/issues/17695) concerns a different
initialization path. Audio Stack uses Espressif's `esp_codec_dev`; establish the
actual register value before considering any change.

This probe cannot measure the analog supply, identify a swapped wire, measure
clock integrity or prove which I2S slot carries microphone samples. Those need
separate hardware or PCM observations. It never writes codec register values.
