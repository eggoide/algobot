"""
Simulated portfolio for backtesting.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional
import datetime


@dataclass
class Position:
    symbol: str
    qty: int
    entry_price: float
    entry_bar: int  # bar index when entered
    entry_time: Optional[datetime.datetime] = None

    @property
    def cost_basis(self) -> float:
        return self.qty * self.entry_price


@dataclass
class Trade:
    timestamp: Optional[datetime.datetime]
    action: str  # BUY or SELL
    symbol: str
    price: float
    qty: int
    pnl: float  # realized PnL (0 for BUY, actual for SELL)
    fee: float
    reason: str = ""
    bar_idx: int = 0
    holding_bars: int = 0


class Portfolio:
    """Simulated portfolio with cash management and position tracking."""

    def __init__(self, initial_cash: float, max_positions: int, fee_per_trade: float = 1.0):
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.max_positions = max_positions
        self.fee = fee_per_trade
        self.positions: Dict[str, Position] = {}
        self.trades: List[Trade] = []
        self.equity_curve: List[float] = []

    @property
    def position_size(self) -> float:
        """Capital allocated per position."""
        return self.initial_cash / self.max_positions if self.max_positions > 0 else self.initial_cash

    @property
    def open_position_count(self) -> int:
        return len(self.positions)

    @property
    def can_buy(self) -> bool:
        return self.open_position_count < self.max_positions

    def has_position(self, symbol: str) -> bool:
        return symbol in self.positions

    def buy(self, symbol: str, price: float, bar_idx: int = 0,
            timestamp: Optional[datetime.datetime] = None, reason: str = "") -> Optional[Trade]:
        """Open a position. Returns Trade or None if cannot buy."""
        if not self.can_buy or symbol in self.positions:
            return None

        qty = int(self.position_size / price)
        if qty <= 0:
            return None

        cost = qty * price + self.fee
        if cost > self.cash:
            return None

        self.cash -= cost
        self.positions[symbol] = Position(
            symbol=symbol,
            qty=qty,
            entry_price=price,
            entry_bar=bar_idx,
            entry_time=timestamp,
        )

        trade = Trade(
            timestamp=timestamp,
            action="BUY",
            symbol=symbol,
            price=price,
            qty=qty,
            pnl=0.0,
            fee=self.fee,
            reason=reason,
            bar_idx=bar_idx,
        )
        self.trades.append(trade)
        return trade

    def sell(self, symbol: str, price: float, bar_idx: int = 0,
             timestamp: Optional[datetime.datetime] = None, reason: str = "") -> Optional[Trade]:
        """Close a position. Returns Trade or None if no position."""
        pos = self.positions.get(symbol)
        if pos is None:
            return None

        revenue = pos.qty * price - self.fee
        self.cash += revenue
        realized_pnl = (price - pos.entry_price) * pos.qty - self.fee
        holding_bars = bar_idx - pos.entry_bar

        trade = Trade(
            timestamp=timestamp,
            action="SELL",
            symbol=symbol,
            price=price,
            qty=pos.qty,
            pnl=realized_pnl,
            fee=self.fee,
            reason=reason,
            bar_idx=bar_idx,
            holding_bars=holding_bars,
        )
        self.trades.append(trade)
        del self.positions[symbol]
        return trade

    def mark_to_market(self, prices: Dict[str, float]) -> float:
        """Calculate total portfolio value at current prices."""
        positions_value = sum(
            pos.qty * prices.get(pos.symbol, pos.entry_price)
            for pos in self.positions.values()
        )
        return self.cash + positions_value

    def record_equity(self, prices: Dict[str, float]):
        """Record current equity for equity curve."""
        self.equity_curve.append(self.mark_to_market(prices))

    def get_sell_trades(self) -> List[Trade]:
        return [t for t in self.trades if t.action == "SELL"]

    def get_buy_trades(self) -> List[Trade]:
        return [t for t in self.trades if t.action == "BUY"]

    def reset(self):
        """Reset portfolio to initial state."""
        self.cash = self.initial_cash
        self.positions.clear()
        self.trades.clear()
        self.equity_curve.clear()
