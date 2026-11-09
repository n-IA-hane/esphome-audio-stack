"""Exercise AW88298 pin code generation and the production GPIO adapter."""

import importlib.util
import subprocess
import sys

import pytest
from test_codec_backend_failure_runtime import BACKEND, ROOT, method

from esphome import config_validation as cv


@pytest.fixture
def aw88298_gain_schema():
    spec = importlib.util.spec_from_file_location("aw88298_schema", BACKEND / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def validate(overrides):
        validated = module.CODEC_OUTPUT_SCHEMA({"type": "aw88298", **overrides})
        return {key: validated[key] for key in ("pa_gain", "pa_voltage", "codec_dac_voltage")}

    return validate


def test_aw88298_gain_defaults_and_units(aw88298_gain_schema):
    assert aw88298_gain_schema({}) == {"pa_gain": 30.0, "pa_voltage": 5.0, "codec_dac_voltage": 3.3}
    assert aw88298_gain_schema({"pa_gain": "24 dB", "pa_voltage": "12 V", "codec_dac_voltage": "1.8 V"}) == {
        "pa_gain": 24.0, "pa_voltage": 12.0, "codec_dac_voltage": 1.8,
    }
    assert aw88298_gain_schema({"pa_gain": "-3 dB"})["pa_gain"] == -3.0


@pytest.mark.parametrize("field,value", [
    ("pa_voltage", 0), ("codec_dac_voltage", -1),
    ("pa_gain", float("nan")), ("pa_gain", float("inf")),
    ("pa_voltage", float("inf")), ("codec_dac_voltage", float("nan")),
])
def test_aw88298_rejects_invalid_gain_values(aw88298_gain_schema, field, value):
    with pytest.raises(cv.Invalid):
        aw88298_gain_schema({field: value})


@pytest.mark.parametrize("pin", ["none", "internal", "expander", "input_only"])
def test_aw88298_pin_codegen(tmp_path, pin):
    text = (ROOT / "tests/components/esp_audio_stack/test.esp32-s3-idf.yaml").read_text()
    text = text.replace("    output:\n      type: es8311", "    output:\n      type: aw88298")
    if pin == "internal":
        text = text.replace("      type: aw88298", "      type: aw88298\n      reset_pin: GPIO0")
    elif pin in ("expander", "input_only"):
        mode = "OUTPUT" if pin == "expander" else "INPUT"
        text = text.replace("      type: aw88298", f"""      type: aw88298
      reset_pin:
        pcf8574: codec_io
        number: 0
        mode: {mode}""")
        text += "\npcf8574:\n  - id: codec_io\n    address: 0x20\n"
    if pin == "expander":
        text = text.replace("      type: aw88298", """      type: aw88298
      pa_gain: 24 dB
      pa_voltage: 12 V
      codec_dac_voltage: 1.8 V""")
    text += f"""
external_components:
  - source:
      type: local
      path: {ROOT / 'esphome/components'}
    components: [esp_audio_stack]
"""
    config = tmp_path / "audio.yaml"
    config.write_text(text)
    result = subprocess.run(
        [sys.executable, "-m", "esphome", "compile", str(config), "--only-generate"],
        capture_output=True, text=True, timeout=60, check=False,
    )
    output = result.stdout + result.stderr
    if pin == "input_only":
        assert result.returncode != 0, output
        assert "output" in output.lower(), output
        assert "Traceback" not in output
        return
    assert result.returncode == 0, output
    generated = next(tmp_path.rglob("main.cpp")).read_text()
    assert "->configure_output_codec(5, 0x36, true, true)" in generated
    assert ("->set_aw88298_reset_pin(" in generated) == (pin != "none")
    gain = "24.0f, 12.0f, 1.8f" if pin == "expander" else "30.0f, 5.0f, 3.3f"
    assert f"->set_aw88298_hw_gain({gain})" in generated
    if pin == "expander":
        assert "pcf8574::PCF8574GPIOPin" in generated
        assert "->set_pin(0)" in generated


def test_aw88298_reset_adapter_lifetime(tmp_path):
    source = (BACKEND / "codec_dev_backend.cpp").read_text()
    header = (BACKEND / "codec_dev_backend.h").read_text()
    header = "\n".join(line for line in header.splitlines() if not line.startswith("#include"))
    fixture = (ROOT / "tests/fixtures/codec_backend_failure.cpp").read_text()
    fixture = fixture[:fixture.index("int main(){")]
    fixture = fixture.replace("struct audio_codec_if_t {", "struct audio_codec_if_t {bool aw_codec=false;")
    fixture = fixture.replace(
        "struct audio_codec_data_if_t {",
        "using esp_codec_dev_i2s_mode_t=int; struct audio_codec_data_if_t {"
        "int (*get_mode)(const audio_codec_data_if_t*,int*,int*);",
    )
    fixture = fixture.replace(
        "if(p)p->get_bus_info=bus_info;",
        "if(p){p->get_bus_info=bus_info;p->get_mode=[](const audio_codec_data_if_t*,int*i,int*o)"
        "{*i=*o=ESP_CODEC_DEV_I2S_MODE_TDM_PHILIPS;return 0;};}",
    )
    fixture = fixture.replace("float channel_gains[4]{};", "unsigned fixture_slot_bits=16; float channel_gains[4]{};")
    fixture = fixture.replace("int order_list(", "bool fixture_missing_order=false; int order_list(")
    fixture = fixture.replace("p->hw_base.get_order_list=order_list;",
                              "p->hw_base.get_order_list=fixture_missing_order?nullptr:order_list;")
    fixture = fixture.replace("*b={16000,16,16,4,5,", "*b={16000,16,fixture_slot_bits,4,5,")
    fixture = fixture.replace(
        "int esp_codec_dev_get_data_layout(Dev*,esp_codec_dev_channel_map_t*m){",
        "int esp_codec_dev_get_data_layout(Dev*d,esp_codec_dev_channel_map_t*m){"
        "if(d->cfg.codec_if->aw_codec)return ESP_CODEC_DEV_NOT_SUPPORT;",
    )
    fixture = fixture.replace(
        "bool CodecDevBackend::make_tx_sample_info_(const SampleConfig&c,esp_codec_dev_sample_info_t&f)"
        "{f=make_sample_info_(c);return true;}", "",
    )
    fixture = fixture.replace("// PRODUCTION_CLASS", '#include "backend.h"')
    fixture = fixture.replace("// PRODUCTION_METHODS", "\n".join(method(source, signature) for signature in [
        "const audio_codec_if_t *CodecDevBackend::new_generic_codec_",
        "bool CodecDevBackend::setup(",
        "bool CodecDevBackend::make_tx_sample_info_(",
        "bool CodecDevBackend::apply_aw88298_tdm_framing_(",
        "bool CodecDevBackend::read_layout_",
        "bool CodecDevBackend::tx_dma_layout_matches",
        "bool CodecDevBackend::open(",
        "float CodecDevBackend::normalize_input_gain(",
        "void CodecDevBackend::set_es7210_config(",
        "void CodecDevBackend::set_input_codec_config(",
        "bool CodecDevBackend::set_input_gain(",
        "bool CodecDevBackend::set_input_channel_gain(",
        "void CodecDevBackend::close()",
        "void CodecDevBackend::destroy_codecs_",
        "void CodecDevBackend::teardown()",
    ]))
    adapter_start = source.index("// The vendor GPIO callbacks")
    adapter = source[adapter_start:source.index("#endif", adapter_start)]
    additions = (ROOT / "tests/fixtures/aw88298_reset.cpp").read_text()
    fixture = fixture.replace("#define private public", additions + "\n#define private public")
    fixture = fixture.replace("CodecDevBackend::~CodecDevBackend()", adapter + "\nCodecDevBackend::~CodecDevBackend()")
    fixture += "\n" + (ROOT / "tests/fixtures/aw88298_reset_main.cpp").read_text()
    (tmp_path / "backend.h").write_text(header)
    cpp = tmp_path / "codec.cpp"
    cpp.write_text(fixture)
    binary = tmp_path / "codec"
    result = subprocess.run([
        "g++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-Wno-unused-parameter",
        "-fsanitize=address,undefined", "-fno-omit-frame-pointer", str(cpp), "-o", str(binary),
    ], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    result = subprocess.run([str(binary)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_aw88298_disabled_keeps_backend_configuration_budget(tmp_path):
    header = (BACKEND / "codec_dev_backend.h").read_text()
    header = "\n".join(line for line in header.splitlines() if not line.startswith("#include"))
    fixture = (ROOT / "tests/fixtures/codec_backend_failure.cpp").read_text()
    fixture = fixture[:fixture.index("// PRODUCTION_CLASS")]
    fixture += r'''
#include "backend.h"
#undef private
#include <type_traits>
template<class T, class = void> struct has_reset_pin : std::false_type {};
template<class T> struct has_reset_pin<T, std::void_t<decltype(T::aw88298_reset_pin_)>> : std::true_type {};
static_assert(sizeof(esphome::esp_audio_stack::CodecDevBackend::GenericCodecConfig) == 16,
              "disabled AW88298 must retain the codec configuration budget");
static_assert(!has_reset_pin<esphome::esp_audio_stack::CodecDevBackend>::value,
              "disabled AW88298 must not retain reset-pin storage");
int main() {}
'''
    (tmp_path / "backend.h").write_text(header)
    cpp = tmp_path / "budget.cpp"
    cpp.write_text(fixture)
    subprocess.run([
        "g++", "-std=c++17", "-Wall", "-Wextra", "-Werror", str(cpp), "-o", str(tmp_path / "budget"),
    ], check=True, capture_output=True, text=True)
