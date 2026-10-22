"""Number platform for ESP Audio Stack - mic gain and Master Volume"""

import esphome.codegen as cg
from esphome.components import number, speaker
import esphome.config_validation as cv
import esphome.final_validate as fv
from esphome.const import (
    CONF_INITIAL_VALUE,
    CONF_ID,
    CONF_MAX_VALUE,
    CONF_MIC_GAIN,
    CONF_MIN_VALUE,
    ENTITY_CATEGORY_CONFIG,
    UNIT_PERCENT,
)

from . import CONF_ESP_AUDIO_STACK_ID, ESPAudioStack, esp_audio_stack_ns

CONF_MASTER_VOLUME = "master_volume"
CONF_SPEAKER_ID = "speaker_id"
CONF_INPUT_PGA = "input_pga"
CONF_REFERENCE_PGA = "reference_pga"

# Number classes
MicGainNumber = esp_audio_stack_ns.class_("MicGainNumber", number.Number, cg.Component)
MasterVolumeNumber = esp_audio_stack_ns.class_(
    "MasterVolumeNumber", number.Number, cg.Component
)
HardwareGainNumber = esp_audio_stack_ns.class_(
    "HardwareGainNumber", number.Number, cg.Component
)
HARDWARE_GAIN_SCHEMA = number.number_schema(
    HardwareGainNumber,
    entity_category=ENTITY_CATEGORY_CONFIG,
    icon="mdi:waveform",
    unit_of_measurement="dB",
).extend({cv.Optional(CONF_INITIAL_VALUE): cv.float_range(min=0, max=37.5)})

MIC_GAIN_SCHEMA = number.number_schema(
    MicGainNumber,
    entity_category=ENTITY_CATEGORY_CONFIG,
    icon="mdi:microphone",
).extend(
    {
        cv.Optional(CONF_MIN_VALUE, default=-20.0): cv.float_range(min=-20.0, max=30.0),
        cv.Optional(CONF_MAX_VALUE, default=30.0): cv.float_range(min=-20.0, max=30.0),
        cv.Optional(CONF_INITIAL_VALUE, default=0.0): cv.float_range(
            min=-20.0, max=30.0
        ),
    }
)


def _validate_mic_gain_range(config):
    if config[CONF_MIN_VALUE] >= config[CONF_MAX_VALUE]:
        raise cv.Invalid("mic_gain min_value must be lower than max_value")
    return config


MIC_GAIN_SCHEMA = cv.All(MIC_GAIN_SCHEMA, _validate_mic_gain_range)

CONFIG_SCHEMA = cv.Schema(
    {
        cv.GenerateID(CONF_ESP_AUDIO_STACK_ID): cv.use_id(ESPAudioStack),
        cv.Optional(CONF_MIC_GAIN): MIC_GAIN_SCHEMA,
        cv.Optional(CONF_INPUT_PGA): HARDWARE_GAIN_SCHEMA,
        cv.Optional(CONF_REFERENCE_PGA): HARDWARE_GAIN_SCHEMA,
        cv.Optional(CONF_MASTER_VOLUME): number.number_schema(
            MasterVolumeNumber,
            entity_category=ENTITY_CATEGORY_CONFIG,
            icon="mdi:volume-high",
            unit_of_measurement=UNIT_PERCENT,
        ).extend(
            {
                cv.Optional(CONF_SPEAKER_ID): cv.use_id(speaker.Speaker),
            }
        ),
    }
)


def _validate_hardware_gains(config):
    if CONF_INPUT_PGA not in config and CONF_REFERENCE_PGA not in config:
        return config
    stacks = fv.full_config.get().get("esp_audio_stack", [])
    if isinstance(stacks, dict):
        stacks = [stacks]
    stack = next(
        (s for s in stacks if s[CONF_ID] == config[CONF_ESP_AUDIO_STACK_ID]),
        stacks[0] if len(stacks) == 1 else {},
    )
    codec = stack.get("codec", {}).get("input", {})
    if not codec:
        raise cv.Invalid(
            "input_pga/reference_pga require a hardware input codec; mic_gain is the digital control"
        )
    if CONF_REFERENCE_PGA in config and (
        codec.get("type") != "es7210" or "ref_channel" not in codec
    ):
        raise cv.Invalid(
            "reference_pga requires ES7210 with a configured physical ref_channel"
        )
    if CONF_REFERENCE_PGA in config and not (
        codec.get("mic_selected", 0x0F) & (1 << codec["ref_channel"])
    ):
        raise cv.Invalid(
            "reference_pga physical ref_channel must be enabled in codec.input.mic_selected"
        )
    return config


FINAL_VALIDATE_SCHEMA = _validate_hardware_gains


async def _setup_mic_gain(config, key, parent):
    if key in config:
        conf = config[key]
        var = await number.new_number(
            conf,
            min_value=conf[CONF_MIN_VALUE],
            max_value=conf[CONF_MAX_VALUE],
            step=1.0,
        )
        await cg.register_component(var, conf)
        cg.add(var.set_parent(parent))
        cg.add(var.set_min_db(conf[CONF_MIN_VALUE]))
        cg.add(var.set_max_db(conf[CONF_MAX_VALUE]))
        cg.add(var.set_initial_db(conf[CONF_INITIAL_VALUE]))


async def to_code(config):
    parent = await cg.get_variable(config[CONF_ESP_AUDIO_STACK_ID])

    await _setup_mic_gain(config, CONF_MIC_GAIN, parent)
    for key in (CONF_INPUT_PGA, CONF_REFERENCE_PGA):
        if key not in config:
            continue
        cg.add_define("USE_ESP_AUDIO_STACK_HARDWARE_GAIN_CONTROL")
        conf = config[key]
        var = await number.new_number(conf, min_value=0, max_value=37.5, step=0.5)
        await cg.register_component(var, conf)
        cg.add(var.set_parent(parent))
        cg.add(var.set_reference(key == CONF_REFERENCE_PGA))
        if CONF_INITIAL_VALUE in conf:
            cg.add(var.set_initial_db(conf[CONF_INITIAL_VALUE]))

    if CONF_MASTER_VOLUME in config:
        conf = config[CONF_MASTER_VOLUME]
        var = await number.new_number(
            conf,
            min_value=0.0,
            max_value=100.0,
            step=5.0,
        )
        await cg.register_component(var, conf)
        cg.add(var.set_parent(parent))
        if CONF_SPEAKER_ID in conf:
            speaker_var = await cg.get_variable(conf[CONF_SPEAKER_ID])
            cg.add(var.set_speaker(speaker_var))
