"""FillReconciler - Reconciles order fills against KIS API and SQLite journal."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any

from core import data_fetcher
from core.ict_journal import ICTJournal

logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")


@dataclass
class FillResult:
    """Result of fill reconciliation."""
    order_no: str
    filled_qty: int
    avg_price: float
    realized_pnl: float
    fees: float
    is_complete: bool


class FillReconciler:
    """Reconciles order fills from KIS API and persists to journal."""

    def __init__(
        self,
        journal: ICTJournal | None = None,
        entry_fee_rate: float = 0.00015,
        exit_fee_rate: float = 0.00015,
        exit_tax_rate: float = 0.0018,
    ):
        self.journal = journal or ICTJournal()
        self.entry_fee_rate = entry_fee_rate
        self.exit_fee_rate = exit_fee_rate
        self.exit_tax_rate = exit_tax_rate

    def reconcile(
        self,
        order_no: str,
        symbol: str,
        side: str,
        quantity: int,
        entry_price: float,
        env_dv: str = "vps",
        stop_price: float | None = None,
    ) -> FillResult:
        """
        Reconcile a closed position using KIS API and calculate actual P&L.

        Args:
            order_no: The order number to reconcile
            symbol: Stock symbol
            side: "buy" or "sell"
            quantity: Target quantity
            entry_price: Entry price (for exit orders, this is the entry price of position)
            env_dv: Environment ("vps" for paper mode)

        Returns:
            FillResult with actual fill information
        """
        filled_qty = 0
        avg_price = 0.0
        fees = 0.0
        realized_pnl = 0.0
        is_complete = False

        fills, fills_ok = data_fetcher.get_order_fills(env_dv)
        if fills_ok and not fills.empty and "order_no" in fills.columns:
            matched = fills[fills["order_no"].astype(str) == str(order_no)]
            if not matched.empty:
                total_value = 0.0
                for _, row in matched.iterrows():
                    row_qty = int(row.get("filled_qty", 0) or 0)
                    row_price = float(row.get("avg_price", 0) or row.get("order_price", 0) or 0)
                    if row_qty <= 0 or row_price <= 0:
                        continue
                    filled_qty += row_qty
                    total_value += row_qty * row_price
                avg_price = total_value / filled_qty if filled_qty > 0 else 0.0
                is_complete = filled_qty >= quantity and quantity > 0 and avg_price > 0

        if not is_complete:
            pending_orders, pending_ok = data_fetcher.get_pending_orders(env_dv)
            if pending_ok and not pending_orders.empty and "order_no" in pending_orders.columns:
                matched = pending_orders[pending_orders["order_no"].astype(str) == str(order_no)]
                if not matched.empty:
                    row = matched.iloc[0]
                    filled_qty = max(filled_qty, int(row.get("filled_qty", 0) or 0))
                    if avg_price <= 0 and filled_qty > 0:
                        avg_price = float(row.get("avg_price", 0) or row.get("order_price", 0) or 0)

        # Calculate realized P&L based on side
        if filled_qty > 0:
            if side == "sell":
                # For sell (exit), entry_price is the position entry, avg_price is exit price
                realized_pnl = (avg_price - entry_price) * filled_qty
            else:
                # For buy, P&L would be calculated on exit
                realized_pnl = 0.0

            turnover = avg_price * filled_qty
            if side == "sell":
                fees = turnover * (self.exit_fee_rate + self.exit_tax_rate)
            else:
                fees = turnover * self.entry_fee_rate

        # Save fill record to journal
        fill_data = {
            "order_no": order_no,
            "symbol": symbol,
            "side": side,
            "filled_qty": filled_qty,
            "avg_price": avg_price,
            "risk_amount": abs(entry_price - stop_price) * quantity if stop_price is not None else None,
            "fees": fees,
            "realized_pnl": realized_pnl,
            "fill_time": datetime.now(KST).isoformat(),
            "is_complete": is_complete,
            "payload_json": {
                "order_no": order_no,
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "filled_qty": filled_qty,
                "avg_price": avg_price,
                "entry_price": entry_price,
                "fees": fees,
                "realized_pnl": realized_pnl,
            },
        }
        self.journal.save_fill(fill_data)

        logger.info(
            "FillReconciler: %s %s qty=%d filled=%d avg=%.0f pnl=%.0f complete=%s",
            side.upper(),
            symbol,
            quantity,
            filled_qty,
            avg_price,
            realized_pnl,
            is_complete,
        )

        return FillResult(
            order_no=order_no,
            filled_qty=filled_qty,
            avg_price=avg_price,
            realized_pnl=realized_pnl,
            fees=fees,
            is_complete=is_complete,
        )
