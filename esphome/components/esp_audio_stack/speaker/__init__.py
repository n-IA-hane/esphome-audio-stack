"""ESP Audio Stack Speaker Platform - Wraps full-duplex bus as standard ESPHome speaker"""

import esphome.codegen as cg
from esphome.components import speaker
import esphome.config_validation as cv
from esphome.const import (
    CONF_BUFFER_DURATION,
    CONF_ID,
    CONF_NEVER,
    CONF_TIMEOUT,
)

from .. import CONF_ESP_AUDIO_STACK_ID, ESPAudioStack, esp_audio_stack_ns
from ..audio_format import final_validate_facade, set_facade_limits

DEPENDENCIES = ["esp_audio_stack"]
CODEOWNERS = ["@n-IA-hane"]

ESPAudioStackSpeaker = esp_audio_stack_ns.class_(
    "ESPAudioStackSpeaker",
    speaker.Speaker,
    cg.Component,
    cg.Parented.template(ESPAudioStack),
)


def _set_stream_limits(config):
    return set_facade_limits(config, microphone=False)


CONFIG_SCHEMA = cv.All(
    speaker.SPEAKER_SCHEMA.extend(
        {
            cv.GenerateID(): cv.declare_id(ESPAudioStackSpeaker),
            cv.GenerateID(CONF_ESP_AUDIO_STACK_ID): cv.use_id(ESPAudioStack),
            cv.Optional(
                CONF_BUFFER_DURATION, default="500ms"
            ): cv.positive_time_period_milliseconds,
            cv.Optional(CONF_TIMEOUT, default=CONF_NEVER): cv.Any(
                cv.positive_time_period_milliseconds,
                cv.one_of(CONF_NEVER, lower=True),
            ),
        }
    ).extend(cv.COMPONENT_SCHEMA),
    _set_stream_limits,
)


def _final_validate(config):
    return final_validate_facade(config, microphone=False)


FINAL_VALIDATE_SCHEMA = _final_validate

async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)

    parent = await cg.get_variable(config[CONF_ESP_AUDIO_STACK_ID])
    cg.add(var.set_parent(parent))
    cg.add(parent.set_speaker_buffer_duration(config[CONF_BUFFER_DURATION]))
    if config[CONF_TIMEOUT] != CONF_NEVER:
        cg.add(var.set_timeout(config[CONF_TIMEOUT]))

    await speaker.register_speaker(var, config)
