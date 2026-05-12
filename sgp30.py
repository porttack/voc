"""
SGP30 VOC/eCO2 sensor driver using raw I2C via smbus2.

The SGP30 uses 16-bit commands (not register-based), so all communication
goes through i2c_rdwr with i2c_msg to avoid SMBus register framing.

Baseline note: measure_iaq() must be called at 1 Hz; the sensor's internal
algorithm depends on this cadence. After ~12 h of continuous operation the
baseline is stable enough to persist across power cycles.
"""

import time
import struct

from smbus2 import SMBus, i2c_msg

SGP30_ADDR = 0x58


def _crc8(data: bytes) -> int:
    crc = 0xFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x31) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def _word_crc(word: int) -> bytes:
    msb = (word >> 8) & 0xFF
    lsb = word & 0xFF
    return bytes([msb, lsb, _crc8([msb, lsb])])


def _parse_words(data: list[int], count: int) -> list[int]:
    words = []
    for i in range(count):
        msb, lsb, crc = data[i * 3], data[i * 3 + 1], data[i * 3 + 2]
        if _crc8([msb, lsb]) != crc:
            raise RuntimeError(f"CRC mismatch on word {i}")
        words.append((msb << 8) | lsb)
    return words


class SGP30:
    def __init__(self, bus: int = 1):
        self._bus = SMBus(bus)

    def _write(self, cmd: int):
        msg = i2c_msg.write(SGP30_ADDR, [(cmd >> 8) & 0xFF, cmd & 0xFF])
        self._bus.i2c_rdwr(msg)

    def _write_bytes(self, payload: bytes):
        msg = i2c_msg.write(SGP30_ADDR, list(payload))
        self._bus.i2c_rdwr(msg)

    def _read(self, n_words: int) -> list[int]:
        msg = i2c_msg.read(SGP30_ADDR, n_words * 3)
        self._bus.i2c_rdwr(msg)
        return _parse_words(list(msg), n_words)

    def iaq_init(self):
        """Start IAQ measurement mode. Call once at startup."""
        self._write(0x2003)
        time.sleep(0.01)

    def measure_iaq(self) -> tuple[int, int]:
        """
        Read eCO2 (ppm) and TVOC (ppb).

        Must be called every 1 s — the SGP30's baseline algorithm assumes this
        cadence. During the first 15 s after iaq_init the sensor returns
        eCO2=400 ppm / TVOC=0 ppb as it warms up.
        """
        self._write(0x2008)
        time.sleep(0.012)
        eco2, tvoc = self._read(2)
        return eco2, tvoc

    def get_baseline(self) -> tuple[int, int]:
        """Return (eCO2_baseline, TVOC_baseline) raw calibration values."""
        self._write(0x2015)
        time.sleep(0.01)
        eco2_base, tvoc_base = self._read(2)
        return eco2_base, tvoc_base

    def set_baseline(self, eco2_baseline: int, tvoc_baseline: int):
        """
        Restore a previously saved baseline.

        Per the SGP30 datasheet the set_baseline payload sends TVOC first,
        then eCO2 — the reverse of the get_baseline response order.
        """
        payload = (
            bytes([(0x20), (0x1E)])
            + _word_crc(tvoc_baseline)
            + _word_crc(eco2_baseline)
        )
        self._write_bytes(payload)
        time.sleep(0.01)

    def close(self):
        self._bus.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
