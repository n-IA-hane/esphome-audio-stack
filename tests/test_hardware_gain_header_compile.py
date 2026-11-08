"""Parse the real public header with an existing IDF environment, no SDK build."""
import json
import os
from pathlib import Path
import shlex
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
HEADER = ROOT / "esphome/components/esp_audio_stack/esp_audio_stack.h"
DATABASES = [Path(p) for p in os.environ.get("AUDIO_STACK_IDF_COMPILE_COMMANDS", "").split(os.pathsep) if p]


def syntax_command(row, translation_unit):
    directory = Path(row["directory"])
    original = Path(row["file"])
    if not original.is_absolute():
        original = directory / original
    args = row.get("arguments") or shlex.split(row["command"])
    result = []
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg in ("-o", "-MF", "-MT", "-MQ"):
            skip = True
            continue
        if arg in ("-c", "-MD", "-MMD", "-MP"):
            continue
        if not arg.startswith("-") and (directory / arg).resolve() == original.resolve():
            continue
        result.append(arg)
    return result + ["-fsyntax-only", "-DCONFIGURED=1", str(translation_unit)]


@pytest.mark.parametrize("database", DATABASES or [None], ids=lambda p: str(p) if p else "no-idf-sdk")
def test_real_header_coexists_with_configured_macro(tmp_path, database):
    if database is None:
        pytest.skip("set AUDIO_STACK_IDF_COMPILE_COMMANDS to existing IDF compile databases")
    rows = json.loads(database.read_text())
    row = next(r for r in rows if r["file"].endswith("/audio_codec_controls.cpp"))
    unit = tmp_path / "gain_header.cpp"
    unit.write_text(f'#include "{HEADER}"\n' + '''
using State = esphome::esp_audio_stack::ESPAudioStack::HardwareGainState;
static_assert(CONFIGURED == 1);
static_assert(static_cast<unsigned>(State::GAIN_CONFIGURED) == 0);
static_assert(static_cast<unsigned>(State::GAIN_WRITE_CONFIRMED) == 1);
static_assert(static_cast<unsigned>(State::GAIN_READ_BACK) == 2);
static_assert(static_cast<unsigned>(State::GAIN_UNKNOWN) == 3);
''')
    command = syntax_command(row, unit)
    (tmp_path / "command.json").write_text(json.dumps({"cwd":row["directory"], "args":command}, indent=2))
    result = subprocess.run(command, cwd=row["directory"], capture_output=True, text=True)
    (tmp_path / "compiler.log").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stderr
