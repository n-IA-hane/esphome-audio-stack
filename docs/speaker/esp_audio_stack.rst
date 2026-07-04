ESP Audio Stack Speaker
=======================

.. seo::
    :description: ESPHome speaker platform exposed by ESP Audio Stack.
    :image: speaker.svg

The ``esp_audio_stack`` speaker platform writes playback audio into the
full-duplex audio backend owned by :doc:`ESP Audio Stack
</components/esp_audio_stack>`. The stack handles bus-rate conversion,
buffering, codec output and the echo-cancellation reference.

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

- **esp_audio_stack_id** (**Required**, :ref:`config-id`): The parent
  ``esp_audio_stack`` component.
- **id** (*Optional*, :ref:`config-id`): Manually specify the speaker ID.
- **buffer_duration** (*Optional*, time): Playback staging buffer size.
- **timeout** (*Optional*, time): Stop the speaker after silence. Defaults to
  no automatic timeout.

See Also
--------

- :doc:`/components/esp_audio_stack`
- :doc:`/components/speaker/index`
- :apiref:`esp_audio_stack/esp_audio_stack.h`
- :ghedit:`Edit`
