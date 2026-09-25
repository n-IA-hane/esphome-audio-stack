"""Run production codec lifecycle methods with deterministic vendor API failures."""

from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "esphome/components/esp_audio_stack"


def method(source, signature):
    start = source.index(signature)
    end = source.index("\n}", start) + 2
    return source[start:end]


@pytest.mark.parametrize("split", [False, True])
def test_codec_backend_failure_ownership_and_retry(tmp_path, split):
    source = (BACKEND / "codec_dev_backend.cpp").read_text()
    header = (BACKEND / "codec_dev_backend.h").read_text()
    # Real class layout, with platform includes supplied by the host fixture.
    header = "\n".join(
        line for line in header.splitlines() if not line.startswith("#include")
    )
    selected = [
        "const audio_codec_if_t *CodecDevBackend::new_generic_codec_",
        "bool CodecDevBackend::setup(",
        "bool CodecDevBackend::read_layout_",
        "bool CodecDevBackend::open(",
        "void CodecDevBackend::close()",
        "void CodecDevBackend::destroy_codecs_",
        "void CodecDevBackend::teardown()",
    ]
    code = (ROOT / "tests/fixtures/codec_backend_failure.cpp").read_text()
    production = "\n".join(method(source, signature) for signature in selected)
    (tmp_path / "backend.h").write_text(header)
    code = code.replace("// PRODUCTION_CLASS", '#include "backend.h"')
    code = code.replace("// PRODUCTION_METHODS", production)
    cpp = tmp_path / "codec.cpp"
    cpp.write_text(code)
    binary = tmp_path / "codec"
    flags = ["-DUSE_ESP_AUDIO_STACK_DUAL_BUS"] if split else []
    subprocess.run(
        [
            "g++",
            "-std=c++17",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-Wno-unused-parameter",
            "-fsanitize=address,undefined",
            "-fno-omit-frame-pointer",
            *flags,
            str(cpp),
            "-o",
            str(binary),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run([str(binary)], check=True, capture_output=True, text=True)
