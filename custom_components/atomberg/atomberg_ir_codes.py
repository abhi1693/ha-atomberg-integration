"""Atomberg IR command definitions.

IR codes are raw captures from an original Atomberg remote, stored as
Broadlink IR packets (base64) and decoded into microsecond timings at import
time. Replaying the captured timings reproduces the exact waveform the remote
sends, including its calibration pulses and NEC repeat frame.
"""

import base64

from infrared_protocols.commands import Command as InfraredCommand
from infrared_protocols.commands.nec import NECCommand

ATOMBERG_IR_MODULATION = 38000

# Raw captures from an original Atomberg remote (Broadlink IR packet format).
ATOMBERG_IR_CODES = {
    "led": "JgBsAAQACE8EAAKIBgAKQQQAAoYEAAUdBAAChwUAB8EFAAvcBgADBwABHpARExESExETERMREhETEhISETYTNRIRExETNhE2EjUTNRI1EzUSNRISEjYSERMREzUSEhISEhISNRMREzUSNRISEgANBQ==",
    "toggle": "JgBQAAABH44TERMREhISERMSEhISEhESEzUSNRMRExIRNhI1EzUSNRM1EhISEhETEjUTERIREzYRExE2EzQTNRISEjYRNhMREgAFQAABHkcTAA0F",
    "one": "JgBUAAUACa8AAR6PEhITERIRExETEhISEhIREhI1EzUTERMREjYSNRI2EjUTNRI2ERISNhISEhETERM1EhISEhI1ExETNBM2ETYSEhMABT0AASFHEgANBQ==",
    "two": "JgBsAAQAAocEAAUeCAACkx0ACh4HAAKQCgAFDAkABPsAAR+PEhITERIRExETEhISERMREhI2EjUTERITETYSNRM0EzUSExESEhISEhI1ExETERM1EjUSNhI1EzQTEhI2ETYTERIABUEAAR5IEQANBQ==",
    "three": "JgCGAAUABRYEAAfeFAAExxIABQQVAAoZBAAC4RoOBwAMgAQACgUEAAKHBAAFHgUAAocTAAo3BQAEBwABH44TEhISERMREhMRExETERIREzYRNhISEhISNRM0EzYRNhISEjUTERM1EhISEhETETYTNBMREzUSEhI1EzUSNRMREwAFQAABH0cSAA0F",
    "four": "JgBkAAUABz4HAAoWBwACtQABH44SEhMREhISERMSEhISEhETETYTNBMRExESNhI1EzUSNRM1EjYREhISEjYSERMREzUSEhISEjUTNRIREzYRNhISEgAFQAABH0cTAAv1AAEfRxMADQU=",
    "five": "JgBQAAABH48SEhISEhIRExIRExETERMREjYSNRMRExETNBM2ETYSNRMRExESEhI2EhIREhISEjYSNRM1EjYREhI2EjUTNRISEgAFQAABH0cTAA0F",
    "boost": "JgBYAAYABRMEAArMAAEfjhMRExETEhISERISEhISExESNRM1EhISEhI1EzUSNRM1EjYRNhM0EzUSEhISEhISNRMRExESEhISEjYRNhM0ExETAAVAAAEeSBIADQU=",
    "timer": "JgBQAAABHZASEhISEhETERMRExIREhISEjYSNRMREhISNhI1EjYSNRMREjYSNRISEzQTERMSEjUSNhESExETNRIREzYRNhISEwAFPwABH0cSAA0F",
    "sleep": "JgBQAAABH44TERMRExETERISEhISEhISETYTNBMRExETNRI2ETYSNRMREzQTNhE2EhITERISEjUTNRISEhIRExE2EzQTNRISEgAFQAABHkcTAA0F",
}


class RawTimingCommand(InfraredCommand):
    """IR command that replays captured raw timings."""

    def __init__(self, timings: list[int], *, modulation: int) -> None:
        """Initialize with raw microsecond timings."""
        super().__init__(modulation=modulation, repeat_count=0)
        self._timings = tuple(timings)

    def get_raw_timings(self) -> list[int]:
        """Return the raw timings (positive=pulse, negative=space)."""
        return list(self._timings)


def _decode_broadlink_packet(packet: bytes, *, tick: float = 32.84) -> list[int]:
    """Decode a Broadlink IR packet into signed microsecond timings."""
    durations: list[int] = []
    index = 4
    end = min(256 * packet[3] + packet[2] + 4, len(packet))
    while index < end:
        chunk = packet[index]
        index += 1
        if chunk == 0:
            chunk = 256 * packet[index] + packet[index + 1]
            index += 2
        durations.append(int(chunk * tick))
    return [
        duration if index % 2 == 0 else -duration
        for index, duration in enumerate(durations)
    ]


def _make_command(code: str) -> RawTimingCommand:
    """Build a raw timing command from a base64 Broadlink IR packet."""
    return RawTimingCommand(
        _decode_broadlink_packet(base64.b64decode(code)),
        modulation=ATOMBERG_IR_MODULATION,
    )


class AtombergIRCommand:
    """Captured IR commands for Atomberg fans."""

    POWER = _make_command(ATOMBERG_IR_CODES["toggle"])
    SPEED_1 = _make_command(ATOMBERG_IR_CODES["one"])
    SPEED_2 = _make_command(ATOMBERG_IR_CODES["two"])
    SPEED_3 = _make_command(ATOMBERG_IR_CODES["three"])
    SPEED_4 = _make_command(ATOMBERG_IR_CODES["four"])
    SPEED_5 = _make_command(ATOMBERG_IR_CODES["five"])
    BOOST = _make_command(ATOMBERG_IR_CODES["boost"])
    SLEEP = _make_command(ATOMBERG_IR_CODES["sleep"])
    LED = _make_command(ATOMBERG_IR_CODES["led"])
    TIMER = _make_command(ATOMBERG_IR_CODES["timer"])


SPEED_MAP = {
    1: AtombergIRCommand.SPEED_1,
    2: AtombergIRCommand.SPEED_2,
    3: AtombergIRCommand.SPEED_3,
    4: AtombergIRCommand.SPEED_4,
    5: AtombergIRCommand.SPEED_5,
    6: AtombergIRCommand.BOOST,
}


# ---------------------------------------------------------------------------
# Efficio+ 400mm Pedestal Swing Fan
# Protocol: Samsung-style NEC variant (38 kHz, 4.5 ms + 4.5 ms header,
# 16-bit frame sent twice without address inversion).
# Codes decoded from Pronto hex captured in issue #52.
# ---------------------------------------------------------------------------

EFFICIO_PLUS_PEDESTAL_IR_ADDRESS = 0x0040


class EfficioPlusPedestalIRCommand:
    """NEC command codes for Atomberg Efficio+ 400mm Pedestal Swing Fan."""

    POWER = NECCommand(
        address=EFFICIO_PLUS_PEDESTAL_IR_ADDRESS,
        command=0x4A,
        modulation=ATOMBERG_IR_MODULATION,
    )
    TOGGLE_SPEED = NECCommand(
        address=EFFICIO_PLUS_PEDESTAL_IR_ADDRESS,
        command=0xC4,
        modulation=ATOMBERG_IR_MODULATION,
    )
    SWING = NECCommand(
        address=EFFICIO_PLUS_PEDESTAL_IR_ADDRESS,
        command=0x38,
        modulation=ATOMBERG_IR_MODULATION,
    )
    TIMER = NECCommand(
        address=EFFICIO_PLUS_PEDESTAL_IR_ADDRESS,
        command=0x15,
        modulation=ATOMBERG_IR_MODULATION,
    )
