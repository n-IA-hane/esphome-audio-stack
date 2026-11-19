"""Build flags for the production speaker write gate with host RTOS primitives."""
from pathlib import Path


_HEADERS = Path(__file__).parent / "fixtures/speaker_sync"
SPEAKER_GATE_FLAGS = ["-I" + str(_HEADERS), "-include", str(_HEADERS / "host.h")]
