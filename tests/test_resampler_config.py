"""Validate explicit scheduling without changing upstream resampler defaults."""

import importlib.util
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
import sys
import pytest
from esphome import config_validation as cv
from esphome.components import esp32

ROOT = Path(__file__).resolve().parents[1]
package_spec = importlib.util.spec_from_file_location("audio_stack_resampler_schema", ROOT / "esphome/components/resampler/__init__.py", submodule_search_locations=[str(ROOT / "esphome/components/resampler")])
package = importlib.util.module_from_spec(package_spec)
sys.modules[package_spec.name] = package
package_spec.loader.exec_module(package)
spec = importlib.util.spec_from_file_location(
    "audio_stack_resampler_schema.speaker",
    ROOT / "esphome/components/resampler/speaker/__init__.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_resampler_defaults_keep_native_scheduling():
    config = module.CONFIG_SCHEMA.validators[0](
        {"id": "resample_test", "output_speaker": "output_test"}
    )
    assert config["task_priority"] == 1
    assert config["task_core"] == -1


def test_resampler_explicit_core_checks_chip_and_priority(monkeypatch):
    from esphome.core import CORE
    monkeypatch.setitem(CORE.data, "core", {"target_platform": "esp32"})
    monkeypatch.setattr(module.fv, "full_config", SimpleNamespace(get=lambda: {"esp32": {"framework": {"sdkconfig_options": {}}}}))
    config = module.CONFIG_SCHEMA.validators[0](
        {
            "id": "resample_test",
            "output_speaker": "output_test",
            "task_core": 1,
            "task_priority": 11,
        }
    )
    with patch.object(esp32, "get_esp32_variant", return_value=esp32.VARIANT_ESP32S3):
        assert module._validate_task_core(config) is config
    with patch.object(esp32, "get_esp32_variant", return_value=esp32.VARIANT_ESP32C3):
        with pytest.raises(cv.Invalid):
            module._validate_task_core(config)
    for priority in (0, 24):
        with pytest.raises(cv.Invalid):
            module.CONFIG_SCHEMA.validators[0](
                {
                    "id": "resample_test",
                    "output_speaker": "output_test",
                    "task_priority": priority,
                }
            )
