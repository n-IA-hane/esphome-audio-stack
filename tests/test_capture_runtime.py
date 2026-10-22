"""Compile the capture component and run its real TCP sender with host RTOS adapters."""

import importlib.util
import json
from pathlib import Path
import socket
import struct
import subprocess
import threading
import wave

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "esphome/components/esp_audio_capture"
spec = importlib.util.spec_from_file_location(
    "capture_audio_tool", ROOT / "tools/capture_audio.py"
)
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


@pytest.fixture(scope="module")
def binary(tmp_path_factory):
    directory = tmp_path_factory.mktemp("capture")
    (directory / "observer.h").write_text(
        "#define USE_ESP_AUDIO_STACK_CAPTURE\n"
        + (
            ROOT / "esphome/components/esp_audio_stack/audio_capture_observer.h"
        ).read_text()
    )
    for source, destination in [
        ("esp_audio_capture.h", "capture.h"),
        ("esp_audio_capture.cpp", "capture.cpp"),
    ]:
        code = (COMPONENT / source).read_text()
        code = "\n".join(
            line
            for line in code.splitlines()
            if not line.startswith('#include "esphome/')
            and not line.startswith("#include <esp_timer")
            and not line.startswith('#include "esp_audio_capture')
        )
        (directory / destination).write_text(code)
    for name in ("main.cpp", "host.h"):
        (directory / name).write_bytes(
            (ROOT / "tests/fixtures/capture" / name).read_bytes()
        )
    exe = directory / "capture"
    result = subprocess.run(
        [
            "g++",
            "-std=c++17",
            "-pthread",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-Wno-misleading-indentation",
            "-Wno-unused-variable",
            "-fsanitize=address,undefined",
            "-fno-omit-frame-pointer",
            str(directory / "main.cpp"),
            "-o",
            str(exe),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return exe


@pytest.mark.parametrize(
    "scenario",
    [
        "happy",
        "mute",
        "generation",
        "format",
        "overflow",
        "reuse",
        "saturated",
        "registry-full",
        "inflight",
    ],
)
def test_capture_socket_and_lifetime(binary, tmp_path, scenario):
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(2)
    server.settimeout(5)
    errors = []
    manifests = []
    release_slow_peer = threading.Event()

    def peer():
        try:
            for attempt in range(2 if scenario == "reuse" else 1):
                connection, _ = server.accept()
                writer = tool.CaptureWriter(tmp_path / str(attempt))
                try:
                    with connection:
                        connection.settimeout(5)
                        if scenario == "overflow":
                            # Deliberately do not drain TCP until the sender has
                            # hit backpressure and finished its bounded stop.
                            connection.setsockopt(
                                socket.SOL_SOCKET, socket.SO_RCVBUF, 4096
                            )
                            release_slow_peer.wait(5)
                        else:
                            tool.receive(connection, writer)
                    writer.close(complete=True)
                except (OSError, ValueError) as error:
                    writer.close(error=error)
                    # Stop deliberately discards the unsent tail, so abrupt
                    # stop/overflow recordings may end on a partial record.
                    if scenario == "happy":
                        errors.append(error)
                manifests.append(writer.manifest)
        except Exception as error:
            errors.append(error)
        finally:
            server.close()

    thread = threading.Thread(target=peer)
    thread.start()
    try:
        result = subprocess.run(
            [str(binary), str(server.getsockname()[1]), scenario],
            capture_output=True,
            text=True,
            timeout=10,
        )
    finally:
        release_slow_peer.set()
    thread.join(6)
    assert not thread.is_alive()
    assert result.returncode == 0, result.stdout + result.stderr
    assert not errors, errors
    if scenario == "happy":
        assert len(manifests[0]["records"]) == 5
        with wave.open(str(tmp_path / "0/raw_mic.wav")) as wav:
            assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (
                48000,
                2,
                4,
            )
            assert struct.unpack("<6i", wav.readframes(3)) == (
                100 * 65536,
                900 * 65536,
                -200 * 65536,
                800 * 65536,
                300 * 65536,
                700 * 65536,
            )
        with wave.open(str(tmp_path / "0/output.wav")) as wav:
            assert wav.getframerate() == 16000
            assert struct.unpack("<3h", wav.readframes(3)) == (100, -200, 300)


def header(**extra):
    return dict(
        v=1,
        point=4,
        seq=0,
        position=0,
        timestamp_us=1,
        generation=1,
        rate=16000,
        frames=2,
        channels=1,
        bits=16,
        dropped=0,
        bytes=4,
        **extra,
    )


def test_capture_connection_failure_has_no_microphone_lease(binary):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    result = subprocess.run(
        [str(binary), str(port), "unreachable"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_receiver_represents_gaps_and_rejects_stale_records(tmp_path):
    writer = tool.CaptureWriter(tmp_path)
    first = header()
    writer.record(first, struct.pack("<2h", 1, 2))
    second = {**first, "seq": 2, "position": 4, "dropped": 1}
    writer.record(second, struct.pack("<2h", 5, 6))
    with pytest.raises(ValueError, match="out-of-order"):
        writer.record(first, b"\0" * 4)
    with pytest.raises(ValueError, match="generation"):
        writer.record({**second, "generation": 2}, b"\0" * 4)
    writer.close()
    with wave.open(str(tmp_path / "output.wav")) as wav:
        assert struct.unpack("<6h", wav.readframes(6)) == (1, 2, 0, 0, 5, 6)
    assert (
        json.loads((tmp_path / "manifest.json").read_text())["streams"]["output"][
            "gap_frames"
        ]
        == 2
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("point", 5),
        ("bits", 24),
        ("channels", 0),
        ("rate", 0),
        ("bytes", 999999),
        ("seq", -1),
    ],
)
def test_receiver_rejects_invalid_metadata(tmp_path, field, value):
    writer = tool.CaptureWriter(tmp_path)
    with pytest.raises(ValueError):
        writer.record({**header(), field: value}, b"\0" * 4)
    writer.close()
