ESP Audio Stack Microphone
==========================

.. seo::
    :description: ESPHome microphone platform exposed by ESP Audio Stack.
    :image: microphone.svg

The ``esp_audio_stack`` microphone platform exposes the capture stream owned by
the :doc:`ESP Audio Stack </components/esp_audio_stack>` component as a normal
ESPHome microphone.

When ``processor_id`` is configured and enabled on the stack, this microphone
is the post-processor stream. If an enabled processor is unavailable, output is
silence. Disabling the parent AEC switch is an explicit bypass that publishes
converted raw mic through this same entity; it does not create a second
microphone path.

The public stream is mono signed 16-bit PCM at the parent's
``output_sample_rate`` (or ``sample_rate`` when omitted). Configure rates on the
parent rather than the microphone platform. A native ``MicrophoneSource`` can
convert channels, sample width and gain; it does not resample this stream.

The native ``AudioCapture`` callback supplies the first sample's capture time
for direct IDF RX without processing or rate conversion. Partial reads preserve
that time; detected DMA loss makes it unknown. CodecDev, resampling and AEC/AFE
output currently report ``0`` (unknown), because their input-to-output timing
has not been established. Callback arrival time is not a capture timestamp.

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

- **esp_audio_stack_id** (*Optional*, :ref:`config-id`): The parent
  ``esp_audio_stack`` component; inferred when exactly one is available.
- **id** (*Optional*, :ref:`config-id`): Manually specify the microphone ID.

See Also
--------

- :doc:`/components/esp_audio_stack`
- :doc:`/components/esp_aec`
- :doc:`/components/esp_afe`
- :doc:`/components/microphone/index`
- :apiref:`esp_audio_stack/esp_audio_stack.h`
- :ghedit:`Edit`
