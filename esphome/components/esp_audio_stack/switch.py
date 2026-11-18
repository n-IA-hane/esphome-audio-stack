"""Switch platform for ESP Audio Stack - AEC enable/disable"""

import esphome.codegen as cg
from esphome.components import switch
import esphome.config_validation as cv
import esphome.final_validate as fv
from esphome.const import ENTITY_CATEGORY_CONFIG

from . import CONF_ESP_AUDIO_STACK_ID, ESPAudioStack, esp_audio_stack_ns

CONF_AEC = "aec"

CONF_ADAPTIVE_VOLUME = "adaptive_volume"
CONF_QUIET_LEVEL = "quiet_level"
CONF_LOUD_LEVEL = "loud_level"
CONF_MAX_REDUCTION = "max_reduction"
AdaptiveVolumeSwitch = esp_audio_stack_ns.class_("AdaptiveVolumeSwitch", switch.Switch, cg.Component)


def _validate_levels(config):
    if config[CONF_LOUD_LEVEL] <= config[CONF_QUIET_LEVEL]:
        raise cv.Invalid("loud_level must be greater than quiet_level (dBFS)")
    return config


# Switch class
AECSwitch = esp_audio_stack_ns.class_("AECSwitch", switch.Switch, cg.Component)

CONFIG_SCHEMA = cv.Schema(
    {
        cv.GenerateID(CONF_ESP_AUDIO_STACK_ID): cv.use_id(ESPAudioStack),
        cv.Optional(CONF_ADAPTIVE_VOLUME): cv.All(
            switch.switch_schema(
                AdaptiveVolumeSwitch,
                block_inverted=True,
                default_restore_mode="RESTORE_DEFAULT_OFF",
                entity_category=ENTITY_CATEGORY_CONFIG,
                icon="mdi:volume-auto",
            ).extend({
                cv.Optional(CONF_QUIET_LEVEL, default=-50): cv.float_range(min=-100, max=0),
                cv.Optional(CONF_LOUD_LEVEL, default=-30): cv.float_range(min=-100, max=0),
                cv.Optional(CONF_MAX_REDUCTION, default=18): cv.float_range(min=0, max=40),
            }),
            _validate_levels,
        ),
        cv.Optional(CONF_AEC): switch.switch_schema(
            AECSwitch,
            entity_category=ENTITY_CATEGORY_CONFIG,
            icon="mdi:account-voice",
        ),
    }
)


def _validate_adaptive_io(config):
    if CONF_ADAPTIVE_VOLUME in config:
        owner = config[CONF_ESP_AUDIO_STACK_ID]
        parent = fv.get_declaring_config(owner)
        switches = fv.platform_entries(
            domain="switch", platform="esp_audio_stack", match={CONF_ESP_AUDIO_STACK_ID: owner}
        )
        if sum(CONF_ADAPTIVE_VOLUME in item for item in switches) > 1:
            raise cv.Invalid("configure one adaptive_volume switch per audio stack")
        if "rx_bus" not in parent and min(parent["i2s_din_pin"], parent["i2s_dout_pin"]) < 0:
            raise cv.Invalid("adaptive_volume requires both microphone input and speaker output")
    return config


FINAL_VALIDATE_SCHEMA = _validate_adaptive_io


async def to_code(config):
    parent = await cg.get_variable(config[CONF_ESP_AUDIO_STACK_ID])

    if CONF_AEC in config:
        conf = config[CONF_AEC]
        var = await switch.new_switch(conf)
        await cg.register_component(var, conf)
        cg.add(var.set_parent(parent))

    if CONF_ADAPTIVE_VOLUME in config:
        conf = config[CONF_ADAPTIVE_VOLUME]
        cg.add_define("USE_ESP_AUDIO_STACK_ADAPTIVE_VOLUME")
        var = await switch.new_switch(conf)
        await cg.register_component(var, conf)
        cg.add(var.set_parent(parent))
        cg.add(parent.configure_adaptive_volume(conf[CONF_QUIET_LEVEL], conf[CONF_LOUD_LEVEL], conf[CONF_MAX_REDUCTION]))
