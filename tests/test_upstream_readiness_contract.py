#!/usr/bin/env python3
"""Static checks for ESPHome-upstream readiness constraints."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = ROOT / "esphome" / "components"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_component_core_does_not_autoload_entity_platforms() -> None:
    for rel in (
        "esp_audio_stack/__init__.py",
        "esp_aec/__init__.py",
        "esp_afe/__init__.py",
    ):
        text = read(COMPONENTS / rel)
        assert "AUTO_LOAD" not in text


def test_upstream_docs_use_placeholder_gpio_pins() -> None:
    docs = list((ROOT / "docs" / "esphome.io").rglob("*.mdx"))
    assert docs

    for path in docs:
        text = read(path)
        for pin in ("GPIO4", "GPIO5", "GPIO6", "GPIO7", "GPIO8", "GPIO47", "GPIO48"):
            assert pin not in text, f"{pin} found in {path.relative_to(ROOT)}"


def test_upstream_code_has_no_product_specific_references() -> None:
    forbidden = ("voip_stack", "intercom", "external_components")

    for path in COMPONENTS.rglob("*"):
        if path.suffix not in {".py", ".cpp", ".h"}:
            continue
        text = read(path)
        lowered = text.lower()
        for token in forbidden:
            assert token.lower() not in lowered, f"{token!r} found in {path.relative_to(ROOT)}"


def test_upstream_mdx_drafts_have_no_external_component_references() -> None:
    docs = list((ROOT / "docs" / "esphome.io").rglob("*.mdx"))
    assert docs

    forbidden = ("external_components", "github://", "n-IA-hane", "esphome-intercom", "voip_stack")

    for path in docs:
        text = read(path)
        lowered = text.lower()
        for token in forbidden:
            assert token.lower() not in lowered, f"{token!r} found in {path.relative_to(ROOT)}"


def test_component_readmes_do_not_document_product_integrations() -> None:
    forbidden = (
        "voip_stack",
        "esphome-intercom",
        "full-experience",
        "ready-to-flash",
        "sendspin",
    )

    for rel in (
        "esp_audio_stack/README.md",
        "esp_aec/README.md",
        "esp_afe/README.md",
    ):
        text = read(COMPONENTS / rel).lower()
        for token in forbidden:
            assert token not in text, f"{token!r} found in esphome/components/{rel}"


def test_audio_stack_support_matrix_is_s3_p4_only() -> None:
    text = read(COMPONENTS / "esp_audio_stack" / "__init__.py")

    assert "VARIANT_ESP32S3: 2" in text
    assert "VARIANT_ESP32P4: 3" in text

    for variant in (
        "VARIANT_ESP32,",
        "VARIANT_ESP32S2",
        "VARIANT_ESP32C3",
        "VARIANT_ESP32C5",
        "VARIANT_ESP32C6",
        "VARIANT_ESP32C61",
        "VARIANT_ESP32H2",
    ):
        assert variant not in text

    assert "requires ESP32-S3 or ESP32-P4 with PSRAM" in text


def test_upstream_yaml_tests_do_not_use_external_components() -> None:
    yamls = list((ROOT / "tests" / "components").rglob("*.yaml"))
    assert yamls

    forbidden = ("external_components", "github://", "n-IA-hane", "voip_stack")

    for path in yamls:
        text = read(path).lower()
        for token in forbidden:
            assert token.lower() not in text, f"{token!r} found in {path.relative_to(ROOT)}"


def test_upstream_yaml_tests_cover_maintained_targets() -> None:
    for component in ("esp_audio_stack", "esp_aec", "esp_afe"):
        base = ROOT / "tests" / "components" / component
        assert (base / "test.esp32-s3-idf.yaml").exists()
        assert (base / "test.esp32-p4-idf.yaml").exists()

        p4_text = read(base / "test.esp32-p4-idf.yaml")
        assert "board: esp32-p4_r3-evboard" in p4_text
        assert "variant: esp32p4" in p4_text

    gmf_text = read(ROOT / "tests" / "components" / "esp_afe" / "test.esp32-p4-gmf-idf.yaml")
    assert "mic_num: 2" in gmf_text
    assert "se_enabled: true" in gmf_text
