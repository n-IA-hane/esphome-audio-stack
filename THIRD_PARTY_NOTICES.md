# Third-Party Notices

Project code in this repository is MIT-licensed unless a file states otherwise.

ESPHome firmware builds resolve most Espressif dependencies through the IDF
Component Manager when the corresponding YAML feature is used:

| Component | Used by |
|---|---|
| `espressif/esp_audio_effects` | Rate, bit-depth and channel conversion. |
| `espressif/esp_codec_dev` | Hardware codec control and codec-backed I2S. |
| `espressif/esp-dsp`, `espressif/esp-sr` | Standalone AEC and AFE support. |
| `espressif/gmf_ai_audio` | GMF-backed AFE path. A minimal local source copy is included under `esphome/components/esp_afe/idf_components/gmf_ai_audio` to keep the upstream GMF AI Audio 1.0.0~1 code while allowing its manifest to resolve `esp-sr` 2.4.6. |

Their upstream licenses apply. Firmware using Espressif-restricted components is
intended for Espressif products/SoCs.

The vendored `gmf_ai_audio` source is licensed under the Espressif Modified MIT
License (`LicenseRef-Espressif-Modified-MIT`). It permits use, copy,
modification, publication, distribution and sublicensing only when used in
conjunction with Espressif Systems products, and its copyright and permission
notice must be included with redistributed copies.
