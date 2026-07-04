# Third-Party Notices

Project code in this repository is MIT-licensed unless a file states otherwise.

This repository does not vendor Espressif source or binary libraries. ESPHome
firmware builds resolve the following IDF Component Manager dependencies when
the corresponding YAML feature is used:

| Component | Used by |
|---|---|
| `espressif/esp_audio_effects` | Rate, bit-depth and channel conversion. |
| `espressif/esp_codec_dev` | Hardware codec control and codec-backed I2S. |
| `espressif/esp-dsp`, `espressif/esp-sr` | Standalone AEC and AFE support. |
| `espressif/gmf_ai_audio` | GMF-backed AFE path. |

Their upstream licenses apply. Firmware using Espressif-restricted components is
intended for Espressif products/SoCs.
