ESP Audio Stack Speaker
=======================

.. seo::
    :description: ESPHome speaker platform exposed by ESP Audio Stack.
    :image: speaker.svg

The ``esp_audio_stack`` speaker platform writes playback audio into the
full-duplex audio backend owned by :doc:`ESP Audio Stack
</components/esp_audio_stack>`. The speaker writes at the parent stack's bus
rate; upstream ESPHome resampler/mixer speakers convert sources that use a
different rate. The stack owns buffering, codec output and the
echo-cancellation reference.

The accepted stream is signed 16-bit PCM at the parent bus rate and playback
channel count. Format declarations must match that parent; they do not add a
converter. Use the native resampler for sources at another rate.

.. code-block:: yaml

    esp_audio_stack:
      id: audio_stack
      sample_rate: 48000
      # bus and codec options...

    speaker:
      - platform: esp_audio_stack
        id: stack_speaker
        esp_audio_stack_id: audio_stack
        buffer_duration: 500ms

Configuration variables:
------------------------

- **esp_audio_stack_id** (*Optional*, :ref:`config-id`): The parent
  ``esp_audio_stack`` component; inferred when exactly one is available.
- **id** (*Optional*, :ref:`config-id`): Manually specify the speaker ID.
- **buffer_duration** (*Optional*, time): Playback staging buffer size, default 500 ms.
- **timeout** (*Optional*, time): Auto-stop after the queue drains and no new PCM
  is written for this interval. Defaults to no automatic timeout.

See Also
--------

- :doc:`/components/esp_audio_stack`
- :doc:`/components/speaker/index`
- :apiref:`esp_audio_stack/esp_audio_stack.h`
- :ghedit:`Edit`
