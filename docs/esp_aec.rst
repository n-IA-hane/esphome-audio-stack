ESP AEC
=======

.. seo::
    :description: Standalone ESP-SR acoustic echo cancellation processor for ESP Audio Stack.
    :image: microphone.svg

The ``esp_aec`` component wraps Espressif ESP-SR acoustic echo cancellation and
implements the audio-processor interface used by
:doc:`esp_audio_stack </components/esp_audio_stack>`.

Use it when the device needs software echo cancellation but not the full AFE
pipeline. It is lighter than ``esp_afe`` and is the normal starting point for a
single-mic Voice Assistant device with media or VoIP playback.

.. code-block:: yaml

    esp_aec:
      id: aec_processor
      mode: sr_low_cost

    esp_audio_stack:
      id: audio_stack
      processor_id: aec_processor
      sample_rate: 48000
      output_sample_rate: 16000
      # bus and codec options...

Configuration variables:
------------------------

- **id** (*Optional*, :ref:`config-id`): Manually specify the processor ID.
- **mode** (*Optional*, string): AEC engine and profile. Supported values are
  ``sr_low_cost``, ``sr_high_perf``, ``fd_low_cost``, ``fd_high_perf``,
  ``voip_low_cost`` and ``voip_high_perf``. Defaults to ``sr_low_cost``.

``sr_*`` modes preserve a wake-word-friendly linear output and are the best
default for assistant devices. ``fd_*`` and ``voip_*`` modes add stronger
full-duplex or communication-oriented suppression and should be validated on
the actual enclosure.

``esp_aec.set_mode`` Action
---------------------------

Switches the AEC mode at runtime.

.. code-block:: yaml

    on_...:
      - esp_aec.set_mode:
          id: aec_processor
          mode: fd_high_perf

See Also
--------

- :doc:`/components/esp_audio_stack`
- :doc:`/components/esp_afe`
- :apiref:`esp_aec/esp_aec.h`
- :ghedit:`Edit`
