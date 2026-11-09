"""Only an enabled TDM reference owns a physical slot."""
import importlib.util
from pathlib import Path

import pytest
from esphome import config_validation as cv

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("tdm_optional_reference_schema", ROOT / "esphome/components/esp_audio_stack/__init__.py")
schema = importlib.util.module_from_spec(spec)
spec.loader.exec_module(schema)


@pytest.mark.parametrize("ref_slot", [1, 7])
def test_disabled_reference_does_not_reserve_or_extend_bus(ref_slot):
    config = {"tdm_mic_slots": [0, 1], "tdm_ref_slot": ref_slot,
              "tdm_total_slots": 2, "use_tdm_reference": False}
    assert schema._validate_tdm_config(config) is config


@pytest.mark.parametrize("ref_slot", [1, 7])
def test_enabled_reference_still_checks_collision_and_bus_bounds(ref_slot):
    with pytest.raises(cv.Invalid):
        schema._validate_tdm_config({"tdm_mic_slots": [0, 1], "tdm_ref_slot": ref_slot,
                                     "tdm_total_slots": 2, "use_tdm_reference": True})


@pytest.mark.parametrize("extra", [{"tdm_mic_slots": [0, 0]}, {"tdm_tx_slot": 2}, {"tdm_mic_slots": [0, 2]}])
def test_disabled_reference_preserves_active_slot_validation(extra):
    with pytest.raises(cv.Invalid):
        schema._validate_tdm_config({"tdm_mic_slots": [0, 1], "tdm_ref_slot": 7,
                                     "tdm_total_slots": 2, "use_tdm_reference": False, **extra})
