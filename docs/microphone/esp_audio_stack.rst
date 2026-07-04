ESP Audio Stack Microphone
==========================

.. seo::
    :description: ESPHome microphone platform exposed by ESP Audio Stack.
    :image: microphone.svg

The ``esp_audio_stack`` microphone platform exposes the capture stream owned by
the :doc:`ESP Audio Stack </components/esp_audio_stack>` component as a normal
ESPHome microphone.

When ``processor_id`` is configured on the stack, this microphone is the
post-processor stream. Wake word, Voice Assistant, VoIP and custom consumers
receive near-end speech after echo cancellation, not the audio currently being
played by the local speaker.

.. code-block:: yaml

    esp_audio_stack:
      id: audio_stack
      sample_rate: 48000
      output_sample_rate: 16000
      # bus and codec options...

    microphone:
      - platform: esp_audio_stack
        id: clean_mic
        esp_audio_stack_id: audio_stack

Configuration variables:
------------------------

- **esp_audio_stack_id** (**Required**, :ref:`config-id`): The parent
  ``esp_audio_stack`` component.
- **id** (*Optional*, :ref:`config-id`): Manually specify the microphone ID.

See Also
--------

- :doc:`/components/esp_audio_stack`
- :doc:`/components/esp_aec`
- :doc:`/components/esp_afe`
- :doc:`/components/microphone/index`
- :apiref:`esp_audio_stack/esp_audio_stack.h`
- :ghedit:`Edit`
