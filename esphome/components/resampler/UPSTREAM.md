# Upstream alignment

Source baseline: ESPHome `dev` commit `2d0e3faa3d1282d1ec6928d4e76b62db3d37434c`.
Speaker scheduling follows PR #21055, patch revision
`71b816a3f6247685415cf49ed911ad6c928d2ea0`.

Both native speaker and microphone resamplers are present. The speaker task
keeps upstream ownership and lifecycle. ESPHome 2026.10.0 still lacks the native
core-affinity create parameter. The creation adapter uses that released API's
known layout; the same StaticTask owns its TCB, stack and teardown. No
old/new API selector or installed-core patch is used.

Bounded-prefix writes retain progress with older ring buffers until PR #21058
is included in the selected release. Stable log declarations remain compatible.
Defaults remain core -1 and priority 1. Selecting a core does not change the
resampling algorithm or imply a measured performance improvement.

The intercom manifest esphome/upstream_sources.json records the shared baseline.
Existing upstream licenses and authorship apply. Builds are separate from
physical-device qualification.
