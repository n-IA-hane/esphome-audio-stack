# Source Snapshot

This file records the initial extraction only. Subsequent changes and snapshots
are identified by commits in this standalone repository.

This repository was extracted from:

- Repository: `n-IA-hane/esphome-intercom`
- Branch: `dev`
- Commit: `6142e9f`
- Extraction date: 2026-07-03

The `main` branch of this repository tracks the stable audio-stack baseline, not an experimental development branch.

## Included From Source

- internal audio core headers under `esphome/components/esp_audio_stack/audio_core_*.h`
- `esphome/components/esp_audio_stack`
- `esphome/components/esp_aec`
- `esphome/components/esp_afe`
- one minimal didactic example

## Current ESPHome boundary

Audio Stack, AEC, AFE and the optional audio-capture component are maintained
here. Standard audio, resampler, mixer, player, Voice Assistant, Micro Wake Word
and OTA providers come from the installed ESPHome release. The former local
resampler provider is retired; its provenance and changes remain in Git history.
