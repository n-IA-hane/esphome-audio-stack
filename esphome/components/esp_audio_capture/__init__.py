"""Opt-in, bounded PCM capture from the existing Audio Stack owner."""

from esphome import automation
import esphome.codegen as cg
import esphome.config_validation as cv
import esphome.final_validate as fv
from esphome.components import psram, socket
from esphome.components.esp_audio_stack import ESPAudioStack, CONF_PROCESSOR_ID
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
CONF_TASK_STACK_IN_PSRAM = "task_stack_in_psram"
CONF_AFE_ID = "afe_id"
CONF_BOUNDARY_PROBES = "boundary_probes"
POINTS = {
    "raw_mic": 0,
    "reference": 1,
    "processor_input": 2,
    "processor_output": 3,
    "output": 4,
    "afe_separated": 5,
    "afe_selected": 6,
}
AFE_POINTS = {"afe_separated", "afe_selected"}
ns = cg.esphome_ns.namespace("esp_audio_capture")
ESPAudioCapture = ns.class_("ESPAudioCapture", cg.Component)
CaptureStartAction = ns.class_("CaptureStartAction", automation.Action)
CaptureStopAction = ns.class_("CaptureStopAction", automation.Action)


def _validate_afe_id(value):
    # Normal capture does not import or require the optional AFE component.
    from esphome.components.esp_afe import EspAfe

    return cv.use_id(EspAfe)(value)


CONFIG_SCHEMA = cv.All(
    cv.Schema(
        {
            cv.GenerateID(): cv.declare_id(ESPAudioCapture),
            cv.Required(CONF_STACK_ID): cv.use_id(ESPAudioStack),
            cv.Required(CONF_MICROPHONE_ID): cv.use_id(ESPAudioStackMicrophone),
            cv.Optional(CONF_AFE_ID): _validate_afe_id,
            cv.Optional(CONF_BOUNDARY_PROBES, default=False): cv.boolean,
            cv.Required(CONF_ADDRESS): cv.ipv4address,
            cv.Optional(CONF_PORT, default=19091): cv.port,
            cv.Optional(
                CONF_POINTS, default=["raw_mic", "reference", "output"]
            ): cv.All(cv.ensure_list(cv.one_of(*POINTS)), cv.Length(min=1, max=7)),
            cv.Optional(CONF_BUFFER_SIZE, default=65536): cv.int_range(
                min=65536, max=262144
            ),
            cv.Optional(CONF_TASK_STACK_IN_PSRAM, default=False): psram.validate_task_stack_in_psram,
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
    afe_requested = bool(AFE_POINTS.intersection(config[CONF_POINTS])) or config[CONF_BOUNDARY_PROBES]
    if afe_requested:
        if CONF_AFE_ID not in config:
            raise cv.Invalid("AFE fetch capture points require afe_id")
        stacks = fv.full_config.get().get("esp_audio_stack", [])
        if isinstance(stacks, dict):
            stacks = [stacks]
        stack = next((s for s in stacks if s[CONF_ID] == config[CONF_STACK_ID]), {})
        if stack.get(CONF_PROCESSOR_ID) != config[CONF_AFE_ID]:
            raise cv.Invalid(
                "capture afe_id must be the selected esp_audio_stack processor_id"
            )
    elif CONF_AFE_ID in config:
        raise cv.Invalid("afe_id is only used with AFE fetch capture points or boundary_probes")
    if config[CONF_BOUNDARY_PROBES] and not fv.full_config.get().get("micro_wake_word"):
        raise cv.Invalid("boundary_probes requires the existing micro_wake_word component")
    return config


FINAL_VALIDATE_SCHEMA = _validate_binding


async def to_code(config):
    cg.add_define("USE_ESP_AUDIO_STACK_CAPTURE")
    if config[CONF_BOUNDARY_PROBES]:
        cg.add_define("USE_ESP_AUDIO_BOUNDARY_PROBES")
        cg.add_build_flag("-Wl,--wrap=FrontendProcessSamples")
        cg.add_build_flag("-Wl,--wrap=esp_afe_handle_from_config")
    if config[CONF_TASK_STACK_IN_PSRAM]:
        psram.request_external_task_stack()
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    parent = await cg.get_variable(config[CONF_STACK_ID])
    cg.add(var.set_parent(parent))
    if AFE_POINTS.intersection(config[CONF_POINTS]):
        cg.add_define("USE_ESP_AUDIO_STACK_AFE_OUTPUT_CAPTURE")
        afe = await cg.get_variable(config[CONF_AFE_ID])
        cg.add(afe.set_raw_capture_parent(parent))
    cg.add(var.set_microphone(await cg.get_variable(config[CONF_MICROPHONE_ID])))
    cg.add(var.set_address(str(config[CONF_ADDRESS])))
    cg.add(var.set_port(config[CONF_PORT]))
    cg.add(var.set_duration(config[CONF_DURATION].total_milliseconds))
    cg.add(var.set_buffer_size(config[CONF_BUFFER_SIZE]))
    cg.add(var.set_task_stack_in_psram(config[CONF_TASK_STACK_IN_PSRAM]))
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
