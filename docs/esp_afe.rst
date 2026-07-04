ESP AFE
=======

.. seo::
    :description: Espressif AFE processor for ESP Audio Stack.
    :image: microphone.svg

The ``esp_afe`` component wraps the Espressif Audio Front End pipeline behind
the audio-processor interface used by
:doc:`esp_audio_stack </components/esp_audio_stack>`.

It provides AEC, noise suppression, VAD, AGC and optional dual-mic Speech
Enhancement/BSS. Use it when a product needs more than standalone echo
cancellation.

.. code-block:: yaml

    esp_afe:
      id: afe_processor
      type: sr
      mode: low_cost
      aec_enabled: true
      ns_enabled: true
      vad_enabled: true

    esp_audio_stack:
      id: audio_stack
      processor_id: afe_processor
      sample_rate: 48000
      output_sample_rate: 16000
      # bus, codec and reference options...

Configuration variables:
------------------------

- **id** (*Optional*, :ref:`config-id`): Manually specify the processor ID.
- **type** (*Optional*, string): AFE profile, ``sr``, ``vc`` or ``fd``.
  Defaults to ``sr``.
- **mode** (*Optional*, string): ``low_cost`` or ``high_perf``. Defaults to
  ``low_cost``.
- **input_format** (*Optional*, string): ESP-SR input layout. Leave ``auto`` for
  normal configs; explicit values include ``mr``, ``mnr``, ``mmr`` and
  ``mmnr``.
- **aec_enabled**, **ns_enabled**, **vad_enabled**, **agc_enabled**
  (*Optional*, boolean): Enable or disable individual AFE stages.
- **mic_num** (*Optional*, int): Number of microphone channels, ``1`` or ``2``.
- **se_enabled** (*Optional*, boolean): Enable dual-mic Speech Enhancement/BSS.

Dual-mic Speech Enhancement requires a TDM topology with two microphone slots
and a valid echo reference supplied by ``esp_audio_stack``.

``esp_afe.set_mode`` Action
---------------------------

Switches the AFE type/mode at runtime.

.. code-block:: yaml

    on_...:
      - esp_afe.set_mode:
          id: afe_processor
          type: fd
          mode: high_perf

See Also
--------

- :doc:`/components/esp_audio_stack`
- :doc:`/components/esp_aec`
- :apiref:`esp_afe/esp_afe.h`
- :ghedit:`Edit`
