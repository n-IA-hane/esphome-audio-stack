"""Opt-in, bounded PCM capture from the existing Audio Stack owner."""

from esphome import automation
import esphome.codegen as cg
import esphome.config_validation as cv
import esphome.final_validate as fv
from esphome.components import socket
from esphome.components.esp_audio_stack import ESPAudioStack
from esphome.components.esp_audio_stack.microphone import ESPAudioStackMicrophone
from esphome.const import CONF_ID, CONF_PORT

CODEOWNERS = ["@n-IA-hane"]
DEPENDENCIES = ["esp_audio_stack", "network"]
AUTO_LOAD = ["socket"]
CONF_STACK_ID = "esp_audio_stack_id"
CONF_MICROPHONE_ID = "microphone_id"
CONF_ADDRESS = "address"
CONF_POINTS = "points"
CONF_DURATION = "duration"
CONF_BUFFER_SIZE = "buffer_size"
POINTS = {
    "raw_mic": 0,
    "reference": 1,
    "processor_input": 2,
    "processor_output": 3,
    "output": 4,
}
ns = cg.esphome_ns.namespace("esp_audio_capture")
ESPAudioCapture = ns.class_("ESPAudioCapture", cg.Component)
CaptureStartAction = ns.class_("CaptureStartAction", automation.Action)
CaptureStopAction = ns.class_("CaptureStopAction", automation.Action)

CONFIG_SCHEMA = cv.All(
    cv.Schema(
        {
            cv.GenerateID(): cv.declare_id(ESPAudioCapture),
            cv.Required(CONF_STACK_ID): cv.use_id(ESPAudioStack),
            cv.Required(CONF_MICROPHONE_ID): cv.use_id(ESPAudioStackMicrophone),
            cv.Required(CONF_ADDRESS): cv.ipv4address,
            cv.Optional(CONF_PORT, default=19091): cv.port,
            cv.Optional(
                CONF_POINTS, default=["raw_mic", "reference", "output"]
            ): cv.All(cv.ensure_list(cv.one_of(*POINTS)), cv.Length(min=1, max=5)),
            cv.Optional(CONF_BUFFER_SIZE, default=65536): cv.int_range(
                min=65536, max=262144
            ),
            cv.Optional(CONF_DURATION, default="15s"): cv.All(
                cv.positive_time_period_milliseconds,
                cv.Range(
                    min=cv.TimePeriod(milliseconds=100), max=cv.TimePeriod(seconds=120)
                ),
            ),
        }
    ).extend(cv.COMPONENT_SCHEMA),
    cv.only_on_esp32,
    socket.consume_sockets(1, "esp_audio_capture"),
)


def _validate_binding(config):
    microphones = fv.full_config.get().get("microphone", [])
    if isinstance(microphones, dict):
        microphones = [microphones]
    microphone = next(
        (m for m in microphones if m[CONF_ID] == config[CONF_MICROPHONE_ID]), {}
    )
    if microphone.get(CONF_STACK_ID) != config[CONF_STACK_ID]:
        raise cv.Invalid(
            "capture microphone_id must belong to the selected esp_audio_stack_id"
        )
    return config


FINAL_VALIDATE_SCHEMA = _validate_binding


async def to_code(config):
    cg.add_define("USE_ESP_AUDIO_STACK_CAPTURE")
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    cg.add(var.set_parent(await cg.get_variable(config[CONF_STACK_ID])))
    cg.add(var.set_microphone(await cg.get_variable(config[CONF_MICROPHONE_ID])))
    cg.add(var.set_address(str(config[CONF_ADDRESS])))
    cg.add(var.set_port(config[CONF_PORT]))
    cg.add(var.set_duration(config[CONF_DURATION].total_milliseconds))
    cg.add(var.set_buffer_size(config[CONF_BUFFER_SIZE]))
    cg.add(
        var.set_points(sum(1 << POINTS[point] for point in set(config[CONF_POINTS])))
    )


@automation.register_action(
    "esp_audio_capture.start",
    CaptureStartAction,
    automation.maybe_simple_id(
        {
            cv.Required(CONF_ID): cv.use_id(ESPAudioCapture),
        }
    ),
    synchronous=True,
)
async def start_to_code(config, action_id, template_arg, args):
    return cg.new_Pvariable(
        action_id, template_arg, await cg.get_variable(config[CONF_ID])
    )


@automation.register_action(
    "esp_audio_capture.stop",
    CaptureStopAction,
    automation.maybe_simple_id(
        {
            cv.Required(CONF_ID): cv.use_id(ESPAudioCapture),
        }
    ),
    synchronous=True,
)
async def stop_to_code(config, action_id, template_arg, args):
    return cg.new_Pvariable(
        action_id, template_arg, await cg.get_variable(config[CONF_ID])
    )
