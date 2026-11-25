#!/usr/bin/env python3
"""Receive opt-in PCM captures, or analyze their existing WAVs entirely offline."""

import argparse
import json
import math
from pathlib import Path
import socket
import struct
import wave

POINTS = (
    "raw_mic", "reference", "processor_input", "processor_output", "output",
    "afe_separated", "afe_selected", "mww_frontend", "afe_feed",
)
MAX_BYTES = 16384


def fnv1a32(pcm):
    value = 2166136261
    for byte in pcm:
        value = ((value ^ byte) * 16777619) & 0xffffffff
    return value


class CaptureWriter:
    def __init__(self, destination, metadata=None):
        self.destination = Path(destination)
        self.destination.mkdir(parents=True, exist_ok=True)
        self.streams = {}
        self.manifest = {"protocol": 1, "complete": False, "streams": {}, "records": []}
        if metadata is not None:
            if not isinstance(metadata, dict):
                raise ValueError("capture metadata must be a JSON object")
            # Snapshot user-supplied gain/board settings without claiming they
            # were measured by the firmware or changing the PCM protocol.
            self.manifest["metadata"] = json.loads(json.dumps(metadata, allow_nan=False))
        self.generation = None

    def record(self, header, pcm):
        fields = (
            "v",
            "point",
            "seq",
            "position",
            "timestamp_us",
            "generation",
            "rate",
            "frames",
            "channels",
            "bits",
            "dropped",
            "bytes",
        )
        if any(type(header.get(key)) is not int or header[key] < 0 for key in fields):
            raise ValueError("invalid capture header fields")
        if (
            header["v"] != 1
            or header["point"] not in range(len(POINTS))
            or header["channels"] not in (1, 2, 3, 4)
            or header["bits"] not in (16, 32)
            or not 8000 <= header["rate"] <= 192000
            or not 0 <= header["bytes"] <= MAX_BYTES
            or header["bytes"] != len(pcm)
            or len(pcm) != header["frames"] * header["channels"] * (header["bits"] // 8)
        ):
            raise ValueError("invalid capture format or payload length")
        if header["point"] in (5, 6) and (
            header["rate"] != 16000 or header["bits"] != 16
            or header["channels"] != (2 if header["point"] == 5 else 1)
        ):
            raise ValueError("invalid AFE output PCM contract")
        boundary = header["point"] in (7, 8)
        if boundary:
            required = ("session", "instance", "offered_frames", "features", "duration_us",
                        "checksum", "checksum_before", "checksum_after", "flags")
            if header.get("boundary") is not True or any(type(header.get(k)) is not int or header[k] < 0 for k in required):
                raise ValueError("invalid boundary metadata")
            if header["rate"] != 16000 or header["bits"] != 16 or (header["channels"] != 1 if header["point"] == 7 else header["channels"] not in (1, 2, 3, 4)):
                raise ValueError("invalid boundary PCM contract")
            if header["point"] == 7 and (header["frames"] > header["offered_frames"] or header["features"] not in (0, 1)):
                raise ValueError("invalid frontend consumed/features contract")
            if header["point"] == 8 and (header["frames"] != header["offered_frames"] or type(header.get("result")) is not int):
                raise ValueError("invalid feed frame/result contract")
            if fnv1a32(pcm) != header["checksum"]:
                raise ValueError("boundary payload checksum mismatch")
            # Preserve flags and before/after checksums even when they report a
            # real input mutation. Offline analysis must classify that evidence.
        elif not header["bytes"] or header["channels"] > 2:
            raise ValueError("invalid standard tap payload")
        if not header["bytes"] and header["point"] != 7:
            raise ValueError("zero payload only valid for frontend consumed-zero")
        if self.generation is None:
            self.generation = header["generation"]
            self.manifest["generation"] = self.generation
        if header["generation"] != self.generation:
            raise ValueError("capture generation changed")
        point = POINTS[header["point"]]
        fmt = (header["channels"], header["bits"] // 8, header["rate"])
        if point not in self.streams:
            wav = wave.open(str(self.destination / f"{point}.wav"), "wb")
            wav.setnchannels(fmt[0])
            wav.setsampwidth(fmt[1])
            wav.setframerate(fmt[2])
            self.streams[point] = {"wave": wav, "format": fmt, "position": 0, "seq": -1}
            self.manifest["streams"][point] = {
                "rate": fmt[2],
                "channels": fmt[0],
                "bits": header["bits"],
                "gap_frames": 0,
            }
        stream = self.streams[point]
        if (
            stream["format"] != fmt
            or header["position"] < stream["position"]
            or header["seq"] <= stream["seq"]
        ):
            raise ValueError("format changed or duplicate/out-of-order stream record")
        gap = header["position"] - stream["position"]
        # A corrupt position must not expand into an unbounded file.
        if gap > 120 * fmt[2] or header["position"] + header["frames"] > 121 * fmt[2]:
            raise ValueError("capture position exceeds maximum duration")
        block = bytes(1024 * fmt[0] * fmt[1])
        remaining = gap
        while remaining:
            frames = min(remaining, 1024)
            stream["wave"].writeframesraw(block[: frames * fmt[0] * fmt[1]])
            remaining -= frames
        stream["wave"].writeframesraw(pcm)
        stream["position"] = header["position"] + header["frames"]
        stream["seq"] = header["seq"]
        self.manifest["streams"][point]["gap_frames"] += gap
        self.manifest["records"].append(header)

    def close(self, complete=False, error=None):
        for stream in self.streams.values():
            stream["wave"].close()
        self.manifest["complete"] = complete
        if error is not None:
            self.manifest["error"] = str(error)
        (self.destination / "manifest.json").write_text(
            json.dumps(self.manifest, indent=2) + "\n"
        )


def receive(connection, writer):
    # Socket file buffers are bounded; each header and PCM frame has its own cap.
    with connection.makefile("rb") as source:
        while True:
            line = source.readline(1025)
            if not line:
                return
            if len(line) > 1024 or not line.endswith(b"\n"):
                raise ValueError("oversized or truncated capture header")
            header = json.loads(line)
            if (
                not isinstance(header, dict)
                or type(header.get("bytes")) is not int
                or not 0 <= header["bytes"] <= MAX_BYTES
            ):
                raise ValueError("invalid capture frame length")
            pcm = source.read(header["bytes"])
            if len(pcm) != header["bytes"]:
                raise ValueError("truncated PCM frame")
            writer.record(header, pcm)


def _dbfs(amplitude):
    return 20 * math.log10(amplitude) if amplitude > 0 else None


def _analyze_stream(destination, point, fmt, records):
    if point not in POINTS or not isinstance(fmt, dict):
        raise ValueError("invalid stream in capture manifest")
    channels, bits, rate = (fmt.get(key) for key in ("channels", "bits", "rate"))
    if (type(channels) is not int or channels not in (1, 2, 3, 4)
            or type(bits) is not int or bits not in (16, 32)
            or type(rate) is not int or not 8000 <= rate <= 192000):
        raise ValueError(f"invalid PCM format for {point}")
    # Integer sums keep 32-bit PCM precise; buffers remain at most 1024 frames.
    stats = [dict(peak=0, total=0, squares=0, saturated=0, near_full_scale=0)
             for _ in range(channels)]
    full_scale = 1 << (bits - 1)
    captured = 0
    last_end = 0
    dropped_max = 0
    with wave.open(str(destination / f"{point}.wav"), "rb") as wav:
        if ((wav.getnchannels(), wav.getsampwidth(), wav.getframerate())
                != (channels, bits // 8, rate) or wav.getcomptype() != "NONE"):
            raise ValueError(f"WAV format disagrees with manifest for {point}")
        total_frames = wav.getnframes()
        if total_frames > 121 * rate:
            raise ValueError(f"WAV exceeds capture duration for {point}")
        unpack = struct.Struct("<" + ("h" if bits == 16 else "i") * channels)
        for record in records:
            position, frames, dropped = (record.get(key) for key in ("position", "frames", "dropped"))
            if (any(type(value) is not int or value < 0 for value in (position, frames, dropped))
                    or position < last_end or position + frames > total_frames
                    or any(record.get(key) != fmt[key] for key in ("channels", "bits", "rate"))):
                raise ValueError(f"invalid record interval or format for {point}")
            wav.setpos(position)
            remaining = frames
            while remaining:
                count = min(remaining, 1024)
                pcm = wav.readframes(count)
                if len(pcm) != count * unpack.size:
                    raise ValueError(f"truncated WAV payload for {point}")
                for frame in unpack.iter_unpack(pcm):
                    for channel, sample in enumerate(frame):
                        stat = stats[channel]
                        amplitude = abs(sample)
                        stat["peak"] = max(stat["peak"], amplitude)
                        stat["total"] += sample
                        stat["squares"] += sample * sample
                        stat["saturated"] += sample in (-full_scale, full_scale - 1)
                        stat["near_full_scale"] += amplitude * 100 >= full_scale * 99
                remaining -= count
            last_end = position + frames
            captured += frames
            dropped_max = max(dropped_max, dropped)
    channel_results = []
    for channel, stat in enumerate(stats):
        peak = stat["peak"] / full_scale
        rms = math.sqrt(stat["squares"] / captured) / full_scale if captured else 0.0
        channel_results.append({
            "channel": channel,
            "samples": captured,
            "peak_pcm": stat["peak"],
            "peak_normalized": peak,
            "peak_dbfs": _dbfs(peak),
            "rms_normalized": rms,
            "rms_dbfs": _dbfs(rms),
            "dc_offset_normalized": stat["total"] / captured / full_scale if captured else None,
            "saturated_samples": stat["saturated"],
            "saturated_fraction": stat["saturated"] / captured if captured else None,
            "near_full_scale_fraction_99pct": stat["near_full_scale"] / captured if captured else None,
        })
    return {
        "rate": rate, "bits": bits, "channels": channels,
        "wav_frames": total_frames, "captured_frames": captured,
        "excluded_gap_frames": total_frames - captured,
        "coverage_fraction": captured / total_frames if total_frames else None,
        "reported_dropped_max": dropped_max,
        "channel_stats": channel_results,
    }


def analyze_capture(destination, metadata=None):
    """Analyze received frames only; zero padding for transport gaps is excluded."""
    destination = Path(destination)
    manifest = json.loads((destination / "manifest.json").read_text())
    if (not isinstance(manifest, dict) or manifest.get("protocol") != 1
            or not isinstance(manifest.get("streams"), dict)
            or not isinstance(manifest.get("records"), list)):
        raise ValueError("invalid capture manifest")
    grouped = {point: [] for point in manifest["streams"]}
    for record in manifest["records"]:
        if (not isinstance(record, dict) or type(record.get("point")) is not int
                or record["point"] not in range(len(POINTS))
                or POINTS[record["point"]] not in grouped):
            raise ValueError("invalid record point in capture manifest")
        grouped[POINTS[record["point"]]].append(record)
    result = {
        "analysis_version": 1,
        "capture_complete": manifest.get("complete") is True,
        "capture_generation": manifest.get("generation"),
        "capture_metadata": manifest.get("metadata", {}),
        "streams": {},
        "level_comparisons": [],
        "interpretation": (
            "Statistics describe received PCM normalized to its WAV container bit depth. "
            "Padding gaps are excluded. Rail hits indicate digital full scale, not proof "
            "of analog clipping. Level differences are unaligned stage summaries, not "
            "AEC ERLE, latency or isolated gain measurements. Null dBFS means silence "
            "or no received samples. User metadata is not firmware telemetry."
        ),
    }
    if "error" in manifest:
        result["capture_error"] = manifest["error"]
    if metadata is not None:
        if not isinstance(metadata, dict):
            raise ValueError("analysis metadata must be a JSON object")
        result["analysis_metadata"] = metadata
    for point, fmt in manifest["streams"].items():
        result["streams"][point] = _analyze_stream(destination, point, fmt, grouped[point])
    for before, after in (("raw_mic", "processor_input"), ("processor_input", "processor_output"),
                          ("processor_output", "output"), ("reference", "processor_output")):
        left, right = (result["streams"].get(point) for point in (before, after))
        if left is None or right is None:
            continue
        comparison = {"from": before, "to": after, "rms_delta_db_by_channel": None}
        if left["channels"] == right["channels"] and left["rate"] == right["rate"]:
            comparison["rms_delta_db_by_channel"] = [
                b["rms_dbfs"] - a["rms_dbfs"]
                if a["rms_dbfs"] is not None and b["rms_dbfs"] is not None else None
                for a, b in zip(left["channel_stats"], right["channel_stats"])
            ]
        else:
            comparison["reason"] = "different channel count or sample rate; no automatic channel mapping"
        result["level_comparisons"].append(comparison)
    (destination / "analysis.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=19091)
    parser.add_argument("--timeout", type=float, default=135)
    parser.add_argument("--analyze", action="store_true",
                        help="analyze existing manifest/WAVs offline; do not open a socket")
    parser.add_argument("--metadata", type=Path,
                        help="JSON object of board/gain settings; retained as user annotations")
    args = parser.parse_args()
    try:
        metadata = json.loads(args.metadata.read_text()) if args.metadata else None
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError("metadata must be a JSON object")
        if args.analyze:
            result = analyze_capture(args.destination, metadata=metadata)
            print(f"Analyzed {len(result['streams'])} PCM streams: {args.destination / 'analysis.json'}")
            return
        writer = CaptureWriter(args.destination, metadata=metadata)
    except (OSError, ValueError, wave.Error) as error:
        raise SystemExit(f"Capture analysis/setup failed: {error}") from error
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((args.bind, args.port))
            server.listen(1)
            server.settimeout(args.timeout)
            print(
                f"Listening on {args.bind}:{args.port}; start esp_audio_capture on the device.",
                flush=True,
            )
            connection, peer = server.accept()
            print(f"Capture connection from {peer[0]}", flush=True)
            with connection:
                connection.settimeout(args.timeout)
                receive(connection, writer)
        writer.close(complete=True)
    except (OSError, ValueError) as error:
        writer.close(error=error)
        raise SystemExit(f"Capture incomplete: {error}") from error


if __name__ == "__main__":
    main()
