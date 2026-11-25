"""Offline measurements use captured samples, never synthetic gap padding."""

import importlib.util
import json
import math
from pathlib import Path
import struct
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("audio_capture_analysis", ROOT / "tools/capture_audio.py")
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


def write_record(writer, point, samples, *, bits=16, channels=1, position=0, seq=0, rate=16000):
    pcm = struct.pack("<" + ("h" if bits == 16 else "i") * len(samples), *samples)
    writer.record({
        "v": 1, "point": tool.POINTS.index(point), "seq": seq, "position": position,
        "timestamp_us": position * 1000000 // rate, "generation": 9, "rate": rate,
        "frames": len(samples) // channels, "channels": channels, "bits": bits,
        "dropped": 2 if position else 0, "bytes": len(pcm),
    }, pcm)


def test_padding_does_not_dilute_peak_rms_or_saturated_fraction(tmp_path):
    writer = tool.CaptureWriter(tmp_path)
    samples = [-32768, 16384, 0, 32767]
    write_record(writer, "processor_input", samples[:2])
    write_record(writer, "processor_input", samples[2:], position=8, seq=1)
    writer.close(complete=True)
    manifest_before = (tmp_path / "manifest.json").read_bytes()
    result = tool.analyze_capture(tmp_path)
    stream = result["streams"]["processor_input"]
    channel = stream["channel_stats"][0]
    assert stream["wav_frames"] == 10 and stream["captured_frames"] == 4
    assert stream["excluded_gap_frames"] == 6 and stream["coverage_fraction"] == 0.4
    assert stream["reported_dropped_max"] == 2
    assert channel["samples"] == 4 and channel["peak_pcm"] == 32768
    assert channel["peak_dbfs"] == 0 and channel["saturated_fraction"] == 0.5
    expected = math.sqrt(sum(sample * sample for sample in samples) / 4) / 32768
    assert channel["rms_normalized"] == pytest.approx(expected)
    assert (tmp_path / "manifest.json").read_bytes() == manifest_before
    assert json.loads((tmp_path / "analysis.json").read_text()) == result


def test_32_bit_stereo_and_silence_remain_independent_and_json_finite(tmp_path):
    writer = tool.CaptureWriter(tmp_path)
    write_record(writer, "raw_mic", [-(2**31), 0, 2**31 - 1, 0], bits=32, channels=2)
    writer.close(error="interrupted capture")
    result = tool.analyze_capture(tmp_path)
    left, right = result["streams"]["raw_mic"]["channel_stats"]
    assert left["peak_pcm"] == 2**31 and left["saturated_fraction"] == 1
    assert left["rms_normalized"] == pytest.approx(1, abs=1e-9)
    assert right["peak_pcm"] == 0 and right["saturated_fraction"] == 0
    assert right["rms_dbfs"] is None and right["peak_dbfs"] is None
    assert result["capture_complete"] is False and result["capture_error"] == "interrupted capture"
    assert "Infinity" not in (tmp_path / "analysis.json").read_text()


def test_stage_level_comparison_and_gain_annotations(tmp_path):
    metadata = {"board": "fixture", "gain": {"mic_pga_db": 18, "speaker_percent": 60},
                "adaptive_volume": {"enabled": True, "ambient_valid": False}}
    writer = tool.CaptureWriter(tmp_path, metadata=metadata)
    metadata["gain"]["mic_pga_db"] = 99
    write_record(writer, "reference", [8192, -8192])
    write_record(writer, "processor_input", [16384, -16384])
    write_record(writer, "processor_output", [8192, -8192])
    write_record(writer, "output", [4096, -4096])
    writer.close(complete=True)
    result = tool.analyze_capture(tmp_path, metadata={"scene": "speaker only"})
    assert result["capture_metadata"]["gain"]["mic_pga_db"] == 18
    assert result["analysis_metadata"] == {"scene": "speaker only"}
    comparisons = {(row["from"], row["to"]): row for row in result["level_comparisons"]}
    assert comparisons[("processor_input", "processor_output")]["rms_delta_db_by_channel"] == pytest.approx([-6.0206], abs=0.0001)
    assert comparisons[("reference", "processor_output")]["rms_delta_db_by_channel"] == [0]
    assert "not AEC ERLE" in result["interpretation"]


def test_different_channel_counts_are_not_silently_mapped(tmp_path):
    writer = tool.CaptureWriter(tmp_path)
    write_record(writer, "raw_mic", [100, 200], channels=2)
    write_record(writer, "processor_input", [100])
    writer.close(complete=True)
    comparison = tool.analyze_capture(tmp_path)["level_comparisons"][0]
    assert comparison["rms_delta_db_by_channel"] is None
    assert "no automatic channel mapping" in comparison["reason"]


@pytest.mark.parametrize("damage", ["overlap", "past_end", "format", "path", "truncate"])
def test_inconsistent_capture_data_is_rejected(tmp_path, damage):
    writer = tool.CaptureWriter(tmp_path)
    write_record(writer, "output", [100, 200])
    writer.close(complete=True)
    path = tmp_path / "manifest.json"
    manifest = json.loads(path.read_text())
    if damage == "overlap":
        manifest["records"].append(dict(manifest["records"][0]))
    elif damage == "past_end":
        manifest["records"][0]["frames"] = 3
    elif damage == "format":
        manifest["streams"]["output"]["bits"] = 32
    elif damage == "path":
        manifest["streams"]["../outside"] = manifest["streams"].pop("output")
    else:
        wav = tmp_path / "output.wav"
        wav.write_bytes(wav.read_bytes()[:-2])
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        tool.analyze_capture(tmp_path)
    assert not (tmp_path / "analysis.json").exists()


def test_offline_cli_never_binds_a_network_socket(tmp_path):
    writer = tool.CaptureWriter(tmp_path)
    write_record(writer, "output", [0, 100])
    writer.close(complete=True)
    # An invalid bind address would fail immediately if --analyze entered capture.
    result = subprocess.run([
        sys.executable, str(ROOT / "tools/capture_audio.py"), str(tmp_path),
        "--analyze", "--bind", "invalid.invalid", "--timeout", "0.01",
    ], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Analyzed 1 PCM streams" in result.stdout
    assert (tmp_path / "analysis.json").exists()
