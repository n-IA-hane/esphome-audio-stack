"""Compile the actual AEC request and admission contracts in host fixtures."""

from pathlib import Path

from test_volume_reference_runtime import method

ROOT = Path(__file__).resolve().parents[1]


def aec_header():
    return (f'#include "{ROOT / "esphome/components/esp_afe/aec_intent.h"}"\n'
            'using AecIntent=esphome::esp_afe::AecIntent;\n')


def admission_methods():
    header = (ROOT / "esphome/components/esp_afe/esp_afe.h").read_text()
    return "\n".join(method(header, f"  bool {name}() const") for name in (
        "processing_owner_current_", "accepting_processing_",
    ))


def ready_admission_fixture():
    # These callback-only fixtures begin with an installed, acknowledged manager.
    return ('AecIntent aec_enabled_{true};\n'
            'EspAfe(){aec_enabled_.acknowledge(aec_enabled_.snapshot());}\n'
            + admission_methods())
