ESP Audio Stack
===============

.. seo::
    :description: Instructions for setting up the ESP Audio Stack, a full-duplex audio backend with echo cancellation support for ESPHome voice devices.
    :image: microphone.svg

The ``esp_audio_stack`` component is a full-duplex audio backend for ESP32-S3
and ESP32-P4 voice devices with PSRAM. It owns the low-level audio path: I2S
buses, hardware codec control, DMA, sample-rate/bit-depth/channel conversion,
playback buffering and the echo-cancellation reference. It exposes standard ESPHome
:doc:`microphone </components/microphone/index>` and
:doc:`speaker </components/speaker/index>` platforms on top.

Use it when microphone and speaker cannot be independent components: shared
codec buses where one codec owns both ADC and DAC, software echo cancellation
that needs a sample-aligned copy of the playback signal on the capture path,
TDM multi-microphone layouts, or devices where media, TTS, wake word, Voice
Assistant and calls share one speaker.

An optional audio processor, :doc:`esp_aec </components/esp_aec>` or
:doc:`esp_afe </components/esp_afe>`, can be attached behind the microphone
surface. When a processor is configured and enabled, the microphone platform
exposes the post-processor stream. If that processor is temporarily
unavailable, output is silence. The optional parent AEC switch is an explicit
bypass that publishes converted raw mic on the same surface; there is no
parallel raw microphone entity.

This component requires the ESP-IDF framework and the ESPHome ``psram``
component. It is not supported on Arduino. Maintained release targets are
ESP32-S3 and ESP32-P4.

.. code-block:: yaml

    # Example configuration entry (single-bus codec)
    i2c:
      sda: GPIO47
      scl: GPIO48

    esp_audio_stack:
      id: audio_stack
      sample_rate: 48000
      output_sample_rate: 16000
      bits_per_sample: 32
      slot_bit_width: 32
      i2s_mclk_pin: GPIO5
      i2s_bclk_pin: GPIO6
      i2s_lrclk_pin: GPIO7
      i2s_din_pin: GPIO4
      i2s_dout_pin: GPIO8
      codec:
        input:
          type: es8311
        output:
          type: es8311

    microphone:
      - platform: esp_audio_stack
        id: stack_mic
        esp_audio_stack_id: audio_stack

    speaker:
      - platform: esp_audio_stack
        id: stack_speaker
        esp_audio_stack_id: audio_stack

Configuration variables:
------------------------

Core audio:

- **id** (*Optional*, :ref:`config-id`): Manually specify the ID used for code generation.
- **sample_rate** (*Optional*, int): Physical I2S bus and speaker sample rate, ``8000`` to ``48000``. Defaults to ``16000``.
- **output_sample_rate** (*Optional*, int): Microphone sample rate exposed to consumers, ``8000`` to ``48000``.
  Must divide ``sample_rate`` exactly, with a maximum decimation ratio of 6. When omitted, no rate
  conversion is performed and the microphone runs at ``sample_rate``.
- **bits_per_sample** (*Optional*, int): Sample container on the bus. One of ``16``, ``24``, ``32``. Defaults to ``16``.
- **slot_bit_width** (*Optional*): Physical slot width on the bus, or ``auto`` to follow
  ``bits_per_sample``. Defaults to ``auto``.
- **num_channels** (*Optional*, int): RX channel count on the physical bus, ``1`` or ``2``. Defaults to ``1``.
- **speaker_channels** (*Optional*, int): Playback channel count, ``1`` or ``2``. Stereo playback requires
  ``num_channels: 2`` and standard I2S (not TDM). Defaults to ``1``.
- **mic_channel** (*Optional*, string): Which stereo slot carries the microphone when RX is stereo,
  ``left`` or ``right``. Defaults to ``left``.
- **rx_slot_mode** (*Optional*, string): Read one or both stereo slots on RX, ``mono`` or ``stereo``.
  Defaults to ``mono``.
- **tx_channel** (*Optional*, string): TX slot placement in mono-on-stereo layouts, ``left`` or ``right``.
  Defaults to ``left``.
- **correct_dc_offset** (*Optional*, boolean): Remove DC bias from the capture path. Defaults to ``false``.
- **input_gain** (*Optional*, float): Digital gain staging applied before the processor, ``0.01`` to
  ``32.0``. Values below ``1.0`` attenuate hot microphones, values above ``1.0`` amplify weak ones.
  Defaults to ``1.0``.
- **master_volume_min_db** (*Optional*, float): Bottom of the volume curve in dB, ``-96.0`` to ``0.0``.
  0% volume remains hard mute. ``-49.0`` matches ESPHome's software volume curve; hardware codec
  outputs default near ``-50 dB`` when this option is omitted.

I2S bus (single-bus mode):

- **i2s_num** (*Optional*, int): I2S peripheral number. Validated against the port count of the target
  SoC variant. Defaults to ``0``.
- **i2s_lrclk_pin** (**Required**, :ref:`config-pin`): Word-select/LRCLK pin. Required unless
  ``rx_bus``/``tx_bus`` are used.
- **i2s_bclk_pin** (**Required**, :ref:`config-pin`): Bit-clock pin. Required unless
  ``rx_bus``/``tx_bus`` are used.
- **i2s_mclk_pin** (*Optional*, :ref:`config-pin`): Master clock pin. Required by most hardware codecs.
- **i2s_din_pin** (*Optional*, :ref:`config-pin`): Data-in pin (microphone/ADC direction).
- **i2s_dout_pin** (*Optional*, :ref:`config-pin`): Data-out pin (speaker/DAC direction).
- **i2s_mode** (*Optional*, string): Clock role, ``primary`` or ``secondary``. Defaults to ``primary``.
- **i2s_comm_fmt** (*Optional*, string): Frame format, one of ``philips``, ``msb``, ``pcm_short``,
  ``pcm_long``. PCM short/long framing is TDM-only in ESP-IDF and is rejected outside TDM mode.
  Defaults to ``philips``.
- **mclk_multiple** (*Optional*, int): MCLK to sample-rate ratio, one of ``128``, ``256``, ``384``,
  ``512``. Defaults to ``256``.
- **use_apll** (*Optional*, boolean): Use the APLL clock source. Among the
  supported targets, this is available on ESP32-P4. Defaults to ``false``.
- **dma_desc_num** (*Optional*, int): DMA descriptor count, ``2`` to ``16``. Defaults to ``6``.
- **dma_frame_num** (*Optional*, int): Frames per DMA descriptor, ``64`` to ``4092``. When omitted, the
  component sizes descriptors at approximately 10 ms each.

Dual-bus mode (separate microphone and speaker peripherals):

- **rx_bus** (*Optional*): Capture bus. Requires **i2s_num**, **i2s_lrclk_pin**, **i2s_bclk_pin**,
  **i2s_din_pin**; **i2s_mclk_pin** optional.
- **tx_bus** (*Optional*): Playback bus. Requires **i2s_num**, **i2s_lrclk_pin**, **i2s_bclk_pin**,
  **i2s_dout_pin**; **i2s_mclk_pin** optional.

``rx_bus`` and ``tx_bus`` must be configured together, on different ``i2s_num`` values, and the
top-level I2S pins must not be set alongside them. Dual-bus mode requires an SoC with at least two
I2S ports and does not support TDM.

Processor and echo-cancellation reference:

- **processor_id** (*Optional*, :ref:`config-id`): ID of an :doc:`esp_aec </components/esp_aec>` or
  :doc:`esp_afe </components/esp_afe>` component to attach behind the microphone surface.
- **aec_reference** (*Optional*, string): Software playback-reference mode used when no hardware
  reference is configured. ``ring_buffer`` (an ADF TYPE2-style reference ring) or ``previous_frame``.
  Defaults to ``ring_buffer``.
- **aec_reference_buffer_ms** (*Optional*, int): Capacity of the software reference ring in
  milliseconds, ``32`` to ``500``. Defaults to ``80``.
- **use_stereo_aec_reference** (*Optional*, boolean): Use stereo codec DAC feedback as the reference
  (for example ES8311 digital loopback, where one input channel is the microphone and the other is
  the DAC output). Defaults to ``false``.
- **reference_channel** (*Optional*, string): Which stereo channel carries the feedback, ``left`` or
  ``right``. Defaults to ``left``.
- **use_tdm_reference** (*Optional*, boolean): Use a TDM input slot as the hardware speaker reference.
  Mutually exclusive with ``use_stereo_aec_reference``. Defaults to ``false``.
- **tdm_total_slots** (*Optional*, int): Total slots in the physical TDM frame, ``2`` to ``8``. Must
  exceed every selected slot index. Defaults to ``4``.
- **tdm_mic_slot** (*Optional*, int): Zero-based microphone slot, ``0`` to ``7``. Defaults to ``0``.
- **tdm_mic_slots** (*Optional*, list): One or two microphone slots for multi-microphone layouts.
  Must not contain duplicates or the reference slot.
- **tdm_ref_slot** (*Optional*, int): Slot carrying the speaker reference, ``0`` to ``7``. Defaults to ``1``.
- **tdm_tx_slot** (*Optional*, int): Playback slot, ``0`` to ``7``. Defaults to ``0``.

Hardware codec (``codec:`` block):

- **input** (*Optional*): ADC-side codec. **type** is one of ``es7210``, ``es8311``, ``es8388``,
  ``es8374``, ``es8389``. Common options: **address** (*Optional*, I2C address; defaults ``0x40`` for
  ES7210, ``0x18`` for ES8311, ``0x20`` otherwise), **gain_db** (*Optional*, float, ``0.0`` to ``37.5``,
  defaults to ``30.0``). ES7210 additionally supports **mic_selected** (channel bitmask, defaults to
  ``0x0F``), **ref_channel** and **ref_gain_db** for hardware reference routing. ES8311/ES8389
  additionally support **use_mclk** (defaults to ``true``) and **no_dac_ref** (defaults to ``false`` on
  input; set to ``false`` to enable the ES8311 digital DAC feedback used by
  ``use_stereo_aec_reference``).
- **output** (*Optional*): DAC-side codec. **type** is one of ``es8311``, ``es8388``, ``es8374``,
  ``es8389``, with the same address and clocking options (``no_dac_ref`` defaults to ``true`` on output).

Task, memory and diagnostics:

- **task_priority** (*Optional*, int): Audio task priority, ``1`` to ``24``. Defaults to ``19``.
- **task_core** (*Optional*, int): Core pinning, ``-1`` (unpinned) to ``1``. Core ``1`` is rejected on
  single-core SoC variants. Defaults to ``0``.
- **task_stack_size** (*Optional*, int): Audio task stack in bytes, ``4096`` to ``32768``. Defaults to ``8192``.
- **buffers_in_psram** (*Optional*, boolean): Place eligible non-DMA audio
  buffers in PSRAM. The amount follows the active topology/frame sizes; DMA
  buffers remain internal. Defaults to ``false``.
- **audio_task_stack_in_psram** (*Optional*, boolean): Place the configured
  audio-task stack in PSRAM. This recovers approximately ``task_stack_size``
  bytes of internal allocation but can increase per-frame latency. It uses
  ESPHome's PSRAM task-stack helper and requires ``psram``. Defaults to
  ``false``.
- **aec_ref_ring_in_psram** (*Optional*, boolean): Place the software AEC
  reference ring in PSRAM. Its size follows the configured rate/frame/capacity;
  internal is faster, while PSRAM recovers that allocation. Defaults to
  ``false``.
- **telemetry** (*Optional*, boolean): Per-stage cycle counting and diagnostics. Debug only, adds
  overhead. Defaults to ``false``.
- **telemetry_log_interval_frames** (*Optional*, int): Telemetry log cadence in frames, ``1`` to
  ``8192``. Defaults to ``128``.
- **audio_effects** (*Optional*): Rate-converter tuning. **rate_cvt_complexity** (int, ``1`` to ``3``,
  defaults to ``3``) and **rate_cvt_perf_type** (``speed`` or ``memory``, defaults to ``speed``).

Automation triggers:

- **on_start** (*Optional*, :ref:`Automation <automation>`): Fired when the stack leaves the idle state.
- **on_idle** (*Optional*, :ref:`Automation <automation>`): Fired when the stack returns to the idle state.
- **on_state** (*Optional*, :ref:`Automation <automation>`): Fired on any runtime state change. The new
  state (``idle``, ``mic``, ``speaker``, ``duplex``) is available as ``x`` (``std::string``).
- **on_mic_start** / **on_mic_idle** (*Optional*, :ref:`Automation <automation>`): Fired when the
  capture path starts or stops.
- **on_speaker_start** / **on_speaker_idle** (*Optional*, :ref:`Automation <automation>`): Fired when
  the playback path starts or stops.
- **on_amplifier_required** / **on_amplifier_idle** (*Optional*, :ref:`Automation <automation>`):
  Semantic aliases of the speaker triggers, intended for boards with a GPIO-controlled amplifier or
  speaker power rail.

Lifecycle
---------

The audio task is created once during component setup and parks while idle. DMA
channels and codec paths start when the first consumer (a microphone listener
or a speaker stream) appears and stop after the last one leaves. Consumers are
reference-counted; the task's TCB/stack remains reserved across those hardware
cycles. Explicit control is rarely needed; the actions below exist for cases
where determinism matters, such as reconfiguring hardware or entering deep
sleep.

.. _esp_audio_stack-start_action:

``esp_audio_stack.start`` Action
--------------------------------

Starts the audio path explicitly.

.. code-block:: yaml

    on_...:
      - esp_audio_stack.start: audio_stack

.. _esp_audio_stack-stop_action:

``esp_audio_stack.stop`` Action
-------------------------------

Requests a stop of the audio path.

.. _esp_audio_stack-stop_and_wait_action:

``esp_audio_stack.stop_and_wait`` Action
----------------------------------------

Stops the audio path and blocks the automation until teardown completes.

.. code-block:: yaml

    on_...:
      - esp_audio_stack.stop_and_wait: audio_stack
      - deep_sleep.enter: sleeper

.. _esp_audio_stack-is_idle_condition:

``esp_audio_stack.is_idle`` Condition
-------------------------------------

Returns true when the stack is in the ``idle`` runtime state.

Switch
------

An optional switch platform exposes runtime AEC control.

.. code-block:: yaml

    switch:
      - platform: esp_audio_stack
        esp_audio_stack_id: audio_stack
        aec:
          name: Echo Cancellation
          restore_mode: RESTORE_DEFAULT_ON

Number
------

An optional number platform exposes gain and volume controls.

.. code-block:: yaml

    number:
      - platform: esp_audio_stack
        esp_audio_stack_id: audio_stack
        mic_gain:
          name: Mic Gain
        master_volume:
          name: Master Volume
          speaker_id: stack_speaker

- **mic_gain** (*Optional*): Microphone gain in dB. **min_value**/**max_value** default to ``-20.0``
  and ``30.0``.
- **master_volume** (*Optional*): Master volume for the speaker referenced by **speaker_id**.

Sensor
------

An optional sensor platform exposes per-TDM-slot signal levels in dBFS (diagnostic entity category,
500 ms default polling). These sensors answer the most common TDM bring-up question empirically:
play audio and watch which slot moves to find the reference slot; speak to find the microphone slots.

.. code-block:: yaml

    sensor:
      - platform: esp_audio_stack
        esp_audio_stack_id: audio_stack
        tdm_slot_levels:
          - slot: 0
            name: TDM Slot 0 Level
          - slot: 2
            name: TDM Slot 2 Level

See Also
--------

- :doc:`/components/microphone/esp_audio_stack`
- :doc:`/components/speaker/esp_audio_stack`
- :doc:`/components/esp_aec`
- :doc:`/components/esp_afe`
- :doc:`/components/voice_assistant`
- :doc:`/components/micro_wake_word`
- :apiref:`esp_audio_stack/esp_audio_stack.h`
- :ghedit:`Edit`
