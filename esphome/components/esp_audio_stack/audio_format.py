"""Exact public PCM formats owned by the single Audio Stack parent."""

from esphome.components import audio
import esphome.config_validation as cv
import esphome.final_validate as fv
from esphome.const import CONF_BITS_PER_SAMPLE, CONF_NUM_CHANNELS, CONF_SAMPLE_RATE
from esphome.core import CORE

from . import CONF_ESP_AUDIO_STACK_ID


def stream_format(parent, *, microphone):
    """Resolve the same defaults as the parent, without depending on schema order."""
    rate = cv.int_range(min=8000, max=48000)(parent.get(CONF_SAMPLE_RATE, 16000))
    if microphone:
        return cv.int_range(min=8000, max=48000)(
            parent.get("output_sample_rate", rate)
        ), 1
    tdm = parent.get("use_tdm_reference", False) or "tdm_mic_slots" in parent
    channels = (
        1 if tdm else cv.one_of(1, 2, int=True)(parent.get("speaker_channels", 1))
    )
    return rate, channels


def set_facade_limits(config, *, microphone, parent=None):
    if parent is None:
        parent = CORE.raw_config.get("esp_audio_stack", {})
        # This component owns one bus. Do not guess between independent parents.
        if isinstance(parent, list):
            if len(parent) != 1:
                raise cv.Invalid("exactly one esp_audio_stack parent is required")
            parent = parent[0]
    rate, channels = stream_format(parent, microphone=microphone)
    if not microphone:
        for key, expected in (
            (CONF_SAMPLE_RATE, rate),
            (CONF_BITS_PER_SAMPLE, 16),
            (CONF_NUM_CHANNELS, channels),
        ):
            if key in config and config[key] != expected:
                raise cv.Invalid(
                    f"{key} must be {expected}, matching the esp_audio_stack parent",
                    path=[key],
                )
        config[CONF_SAMPLE_RATE] = rate
        config[CONF_BITS_PER_SAMPLE] = 16
        config[CONF_NUM_CHANNELS] = channels
    audio.set_stream_limits(
        min_bits_per_sample=16,
        max_bits_per_sample=16,
        min_channels=channels,
        max_channels=channels,
        min_sample_rate=rate,
        max_sample_rate=rate,
    )(config)
    return config


def final_validate_facade(config, *, microphone):
    # Re-resolve the declared ID after all schemas, including consumer limits.
    def validate_parent(parent):
        set_facade_limits(config, microphone=microphone, parent=parent)
        return parent

    fv.id_declaration_match_schema(validate_parent)(config[CONF_ESP_AUDIO_STACK_ID])
    return config
