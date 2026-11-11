from __future__ import annotations

from pathlib import Path
from typing import Any

from .drone_config import DroneConfig
from .formula_fmi import FormulaFMU
from .domain_twin import DomainTwin


class TwinsLayer2:
    """Own independent MESA-NAV and WingMPET FMI instances.

    No scheduling, topology, coupling, network weight, or Center-of-Consistency
    policy is implemented here. Each twin can be managed independently.
    """

    def __init__(
        self,
        mesa_fmu: str | Path,
        wingmpet_fmu: str | Path,
        *,
        config: str | Path | DroneConfig | None = None,
        tddt_packets: bool = False,
        **fmu_options: Any,
    ) -> None:
        self.config = config if isinstance(config, DroneConfig) else (DroneConfig(config) if config is not None else None)
        cls = DomainTwin if tddt_packets else FormulaFMU
        self.mesa = cls(mesa_fmu, config=self.config, instance_name="MESA_NAV_library", **fmu_options)
        try:
            self.wingmpet = cls(wingmpet_fmu, config=self.config, instance_name="WingMPET_library", **fmu_options)
        except Exception:
            self.mesa.close()
            raise

    @classmethod
    def tddt(cls, mesa_fmu, wingmpet_fmu, *, config=None, **opts):
        return cls(mesa_fmu, wingmpet_fmu, config=config, tddt_packets=True, **opts)

    def close(self) -> None:
        self.wingmpet.close()
        self.mesa.close()

    def __enter__(self) -> "TwinsLayer2":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()
