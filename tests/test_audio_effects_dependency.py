"""Exercise the production dependency selector, preserving pre-v3 P4 binaries."""
import ast
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from esphome.components.esp32.const import VARIANT_ESP32P4

ROOT = Path(__file__).resolve().parents[1]


def selected_dependency(variant, chip_config):
    tree = ast.parse((ROOT / "esphome/components/esp_audio_stack/__init__.py").read_text())
    default = next(node for node in tree.body if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == "ESP_AUDIO_EFFECTS_REF"
                           for target in node.targets))
    codegen = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "to_code")
    start = next(index for index, node in enumerate(codegen.body) if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id == "audio_effects_ref" for target in node.targets))
    end = next(index for index in range(start, len(codegen.body))
               if isinstance(codegen.body[index], ast.Expr)
               and isinstance(codegen.body[index].value, ast.Call)
               and any(keyword.arg == "name" and isinstance(keyword.value, ast.Constant)
                       and keyword.value.value == "espressif/esp_audio_effects"
                       for keyword in codegen.body[index].value.keywords))
    original = deepcopy(chip_config)
    core = SimpleNamespace(config={"esp32": chip_config})
    requests = []
    namespace = {"ESP_AUDIO_EFFECTS_REF": ast.literal_eval(default.value),
                 "CORE": core, "VARIANT_ESP32P4": VARIANT_ESP32P4,
                 "get_esp32_variant": lambda: variant,
                 "add_idf_component": lambda **request: requests.append(request)}
    # Execute the actual selector statements, not a mirrored implementation.
    exec(compile(ast.Module(body=codegen.body[start:end + 1], type_ignores=[]),
                 "audio-effects-dependency-selector", "exec"), namespace)
    assert chip_config == original, "A library update must not change chip revision settings"
    assert len(requests) == 1
    return requests[0]


@pytest.mark.parametrize("variant,chip_config,expected", [
    ("ESP32S3", {}, "1.4.4"),
    ("ESP32S3", {"engineering_sample": True}, "1.4.4"),
    ("ESP32", {}, "1.4.4"),
    (VARIANT_ESP32P4, {}, "1.4.4"),
    (VARIANT_ESP32P4, {"engineering_sample": False}, "1.4.4"),
    (VARIANT_ESP32P4, {"engineering_sample": True}, "1.3.0~1"),
])
def test_effects_pin_uses_current_binaries_except_legacy_p4(variant, chip_config, expected):
    assert selected_dependency(variant, chip_config) == {
        "name": "espressif/esp_audio_effects", "ref": expected,
    }
