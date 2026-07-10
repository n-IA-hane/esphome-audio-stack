# Third-Party Notices

Project code in this repository is MIT-licensed unless a file states otherwise.

ESPHome firmware builds resolve most Espressif dependencies through the IDF
Component Manager when the corresponding YAML feature is used:

| Component | Used by |
|---|---|
| `espressif/esp_audio_effects` | Rate, bit-depth and channel conversion. |
| `espressif/esp_codec_dev` | Hardware codec control and codec-backed I2S. |
| `espressif/esp-dsp`, `espressif/esp-sr` | Standalone AEC and AFE support. |
| `espressif/gmf_ai_audio` | Dual-mic GMF-backed AFE path. ESPHome fetches `elements/gmf_ai_audio` from `https://github.com/n-IA-hane/esp-gmf.git` at ref `gmf-ai-audio-esp-sr-2.4.6`; it is not vendored in this repository. |

Their upstream licenses apply. Firmware using Espressif-restricted components is
intended for Espressif products/SoCs.

The fetched `gmf_ai_audio` source carries the license terms in its upstream/fork
tree, including Espressif product-use restrictions where applicable. Review and
redistribute those notices with any firmware or source distribution that
includes the dependency.
