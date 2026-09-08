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

Single-mic and dual-mic profiles use the GMF AFE manager/pipeline. Its feed
worker owns DSP processing, while the parent audio task retains its hardware
cadence. Input staging and the existing output byte stream adapt changes in
ESP-SR block sizes without resizing the live I2S/DMA frame.

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
- **aec_filter_length** (*Optional*, int): ESP-SR filter-length parameter,
  ``1`` to ``8``. Defaults to ``4``; effective time coverage is engine
  dependent.
- **aec_nlp_level** (*Optional*, string): ``normal``, ``aggressive`` or
  ``very_aggressive``. Defaults to ``aggressive``.
- **mic_num** (*Optional*, int): Number of microphone channels, ``1`` or ``2``.
- **se_enabled** (*Optional*, boolean): Enable dual-mic Speech Enhancement/BSS.
- **vad_mode**, **vad_min_speech_ms**, **vad_min_noise_ms**, **vad_delay_ms**
  (*Optional*): VAD aggressiveness and transition timing.
- **vad_mute_playback**, **vad_enable_channel_trigger**, **continuous_vad**
  (*Optional*, boolean): Optional VAD playback/channel/standby behavior.
- **agc_compression_gain**, **agc_target_level** (*Optional*, int): AGC level
  controls. NS/AGC changes rebuild the AFE. ESP-SR 2.5.3 omits AGC from its
  effective dual-microphone graph, so dual-mic profiles apply the public
  WebRTC AGC to complete processed mono frames after the AFE. This adds a
  fixed 10 ms causal delay.
- **memory_alloc_mode** (*Optional*, string): ``more_internal``,
  ``internal_psram_balance`` or ``more_psram``. Defaults to ``more_psram``.
- **afe_linear_gain** (*Optional*, float): Output multiplier, ``0.1`` to
  ``10.0``. Defaults to ``1.0``.
- **ringbuf_size** (*Optional*, int): Requested ESP-SR ring size, ``2`` to
  ``32``. The single-mic configuration normalizes values below ``16`` to ``16``.
- **task_core** / **task_priority** (*Optional*, int): ESP-SR SE/BSS worker
  placement.
- **feed_task_core**, **feed_task_priority**, **feed_task_stack_size** and the
  matching ``fetch_task_*`` options (*Optional*, int): Dual-mic GMF manager/pipeline
  task settings.
- **feed_buf_in_psram**, **feed_ring_in_psram**, **fetch_ring_in_psram**
  (*Optional*, boolean): Wrapper scratch/bridge placement controls.
- **output_prebuffer_frames** (*Optional*, int): Keep ``0`` to preserve the
  zero-reserve bridge. Values ``1`` or ``2`` add fixed processed-audio latency
  on the asynchronous dual-mic GMF path so bounded scheduler jitter does not
  become a silent output frame.

Dual-mic Speech Enhancement requires a TDM topology with two microphone slots
and a valid echo reference supplied by ``esp_audio_stack``.

``esp_afe.set_mode`` Action
---------------------------

Queues an AFE type/mode rebuild. Stop and wait for ``esp_audio_stack`` first;
then wait for ``is_reconfigure_idle()`` and check
``get_last_reconfigure_ok()`` before restarting audio. Rebuild duration is
target dependent and can extend to hundreds of milliseconds on a dual-mic GMF
graph.

.. code-block:: yaml

    on_...:
      - esp_afe.set_mode:
          id: afe_processor
          mode: fd_high_perf

See Also
--------

- :doc:`/components/esp_audio_stack`
- :doc:`/components/esp_aec`
- :apiref:`esp_afe/esp_afe.h`
- :ghedit:`Edit`
