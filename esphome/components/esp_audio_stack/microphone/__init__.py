"""ESP Audio Stack Microphone Platform - Wraps full-duplex bus as standard ESPHome microphone"""

import esphome.codegen as cg
from esphome.components import microphone
import esphome.config_validation as cv
from esphome.const import (
    CONF_BITS_PER_SAMPLE,
    CONF_ID,
    CONF_NUM_CHANNELS,
    CONF_SAMPLE_RATE,
)

from .. import CONF_ESP_AUDIO_STACK_ID, ESPAudioStack, esp_audio_stack_ns
from ..audio_format import final_validate_facade, set_facade_limits

DEPENDENCIES = ["esp_audio_stack"]
CODEOWNERS = ["@n-IA-hane"]

ESPAudioStackMicrophone = esp_audio_stack_ns.class_(
    "ESPAudioStackMicrophone",
    microphone.Microphone,
    cg.Component,
    cg.Parented.template(ESPAudioStack),
)


def _set_stream_limits(config):
    return set_facade_limits(config, microphone=True)


def _reject_child_audio_overrides(config):
    """The audio stack microphone format is owned by the shared full-duplex bus.

    `microphone.MICROPHONE_SCHEMA` inherits the generic audio keys, but applying
    them on the child would be misleading: the C++ stream format is derived from
    the parent `esp_audio_stack` bus and `output_sample_rate`.
    """
    invalid = [
        key
        for key in (CONF_SAMPLE_RATE, CONF_BITS_PER_SAMPLE, CONF_NUM_CHANNELS)
        if key in config
    ]
    if invalid:
        fields = ", ".join(invalid)
        raise cv.Invalid(
            f"{fields} must be configured on esp_audio_stack, not on "
            "microphone.platform: esp_audio_stack. The audio stack microphone "
            "publishes the parent output stream."
        )
    return config


CONFIG_SCHEMA = cv.All(
    microphone.MICROPHONE_SCHEMA.extend(
        {
            cv.GenerateID(): cv.declare_id(ESPAudioStackMicrophone),
            cv.GenerateID(CONF_ESP_AUDIO_STACK_ID): cv.use_id(ESPAudioStack),
        }
    ).extend(cv.COMPONENT_SCHEMA),
    _reject_child_audio_overrides,
    _set_stream_limits,
)


def _final_validate(config):
    return final_validate_facade(config, microphone=True)


FINAL_VALIDATE_SCHEMA = _final_validate

async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    await microphone.register_microphone(var, config)

    parent = await cg.get_variable(config[CONF_ESP_AUDIO_STACK_ID])
    cg.add(var.set_parent(parent))
