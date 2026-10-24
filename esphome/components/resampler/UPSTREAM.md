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

An additional local code-generation hint recognizes an explicitly mono speaker
feeding an actual mono Mixer renderer. When the coordinated owned audio header
advertises `ESPHOME_AUDIO_HAS_MONO_CHANNEL_PRUNING`, the facade permits pruning
the right DSP lane that the renderer would discard. The audio wrapper preserves
the existing stereo layout, partial-byte writes, frame credits and gain-ramp
progression. It selects the left channel, without averaging. Equal-rate,
equal-bit-depth playback keeps the task-free bypass; ordinary stereo output
retains both channels. With the official ESPHome audio header the facade keeps
using its native four-argument API and does not enable this optimization.

This hint is a separate local adaptation, not part of task-affinity PR #21055.
The coordinated Intercom test `test_resampler_channel_pruning_runtime.py` uses
the real ART/PCM/mixer/gain implementation and also compiles against the official
header. Schema tests verify that ordinary sinks, stereo Mixers and inherited
channel defaults do not accidentally enable the hint. No vendor patch, user
option or extra task is required.

The intercom manifest esphome/upstream_sources.json records the shared baseline.
Existing upstream licenses and authorship apply. Builds are separate from
physical-device qualification.
