# notifications/cycle_tracker.py
from typing import Any, Dict, Optional


class CyclePnlTracker:
    """
    Placeholder para PnL por balances (Pac+Lig).
    Por ahora no implementa balances reales: devuelve None.
    (Vos dijiste que luego investigarás cómo obtener los saldos.)

    Igual mantenemos la interfaz:
      - snapshot_open(pac_client, lig_client)
      - snapshot_close_and_pnl(pac_client, lig_client) -> dict
    """

    def __init__(self):
        self._before_sum: Optional[float] = None

    def snapshot_open(self, pac_client: Any, lig_client: Any) -> None:
        # TODO: implementar sumatoria real de balances
        self._before_sum = None

    def snapshot_close_and_pnl(self, pac_client: Any, lig_client: Any) -> Dict[str, Optional[float]]:
        # TODO: implementar sumatoria real de balances
        after_sum = None
        pnl = None
        if self._before_sum is not None and after_sum is not None:
            pnl = after_sum - self._before_sum

        return {
            "before_sum": self._before_sum,
            "after_sum": after_sum,
            "pnl": pnl,
        }