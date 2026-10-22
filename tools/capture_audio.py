#!/usr/bin/env python3
"""Receive Audio Stack's opt-in PCM stream and preserve gaps in separate WAVs."""

import argparse
import json
from pathlib import Path
import socket
import wave

POINTS = ("raw_mic", "reference", "processor_input", "processor_output", "output")
MAX_BYTES = 16384


class CaptureWriter:
    def __init__(self, destination):
        self.destination = Path(destination)
        self.destination.mkdir(parents=True, exist_ok=True)
        self.streams = {}
        self.manifest = {"protocol": 1, "complete": False, "streams": {}, "records": []}
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
            or header["channels"] not in (1, 2)
            or header["bits"] not in (16, 32)
            or not 8000 <= header["rate"] <= 192000
            or not 0 < header["bytes"] <= MAX_BYTES
            or header["bytes"] != len(pcm)
            or len(pcm) != header["frames"] * header["channels"] * (header["bits"] // 8)
        ):
            raise ValueError("invalid capture format or payload length")
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
            line = source.readline(513)
            if not line:
                return
            if len(line) > 512 or not line.endswith(b"\n"):
                raise ValueError("oversized or truncated capture header")
            header = json.loads(line)
            if (
                not isinstance(header, dict)
                or type(header.get("bytes")) is not int
                or not 0 < header["bytes"] <= MAX_BYTES
            ):
                raise ValueError("invalid capture frame length")
            pcm = source.read(header["bytes"])
            if len(pcm) != header["bytes"]:
                raise ValueError("truncated PCM frame")
            writer.record(header, pcm)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=19091)
    parser.add_argument("--timeout", type=float, default=135)
    args = parser.parse_args()
    writer = CaptureWriter(args.destination)
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
