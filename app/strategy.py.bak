"""
Strategy module - abstract base + concrete strategies.
Decoupled from execution so strategies can be used in both live trading and backtesting.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any

import numpy as np
import pandas as pd

from indicators import rsi_wilder, macd, bollinger_bands, sma, volume_sma


@dataclass
class Signal:
    symbol: str
    price: float
    action: str  # "BUY" or "SELL"
    reason: str = ""
    strength: float = 0.0  # 0-1, higher = stronger signal
    indicators: Dict[str, float] = field(default_factory=dict)


@dataclass
class ExitSignal:
    action: str = "SELL"
    reason: str = ""
    indicators: Dict[str, float] = field(default_factory=dict)


class Strategy(ABC):
    """Abstract base strategy."""

    def __init__(self, params: Dict[str, Any]):
        self.params = params

    @abstractmethod
    def generate_signals(self, data: Dict[str, pd.DataFrame], existing_positions: List[str]) -> List[Signal]:
        """Scan market data and return buy signals."""
        ...

    @abstractmethod
    def should_exit(self, symbol: str, entry_price: float, current_price: float,
                    holding_bars: int, data: Optional[pd.DataFrame] = None) -> Optional[ExitSignal]:
        """Check if a position should be closed."""
        ...

    @property
    def name(self) -> str:
        return self.__class__.__name__


class DipBuyStrategy(Strategy):
    """
    Original strategy from bot.py:
    - Buy when RSI < limit AND price dropped >= buy_drop from reference.
    - Sell at fixed take-profit or stop-loss.
    """

    def __init__(self, params: Dict[str, Any]):
        super().__init__(params)
        self.buy_drop = params.get("buy_drop", 0.02)
        self.sell_gain = params.get("sell_gain", 0.03)
        self.rsi_limit = params.get("rsi_limit", 30)
        self.rsi_period = params.get("rsi_period", 14)
        self.use_stop_loss = params.get("use_stop_loss", False)
        self.stop_loss = params.get("stop_loss", 0.15)
        self.dip_mode = params.get("dip_mode", "DAILY")
        self.use_sma_filter = params.get("use_sma_filter", False)
        self.sma_period = params.get("sma_period", 200)

    def _get_reference_price(self, df: pd.DataFrame) -> Optional[float]:
        if len(df) < 2:
            return None
        if self.dip_mode == "DAILY":
            last_bar_date = df.index[-1].date() if hasattr(df.index[-1], 'date') else None
            if last_bar_date is not None:
                prev_days = df[df.index.date < last_bar_date]
                if not prev_days.empty:
                    return float(prev_days['Close'].iloc[-1])
            return float(df['Close'].iloc[-2])
        else:
            return float(df['Close'].iloc[-2])

    def generate_signals(self, data: Dict[str, pd.DataFrame], existing_positions: List[str]) -> List[Signal]:
        signals = []
        for symbol, df in data.items():
            if symbol in existing_positions:
                continue
            try:
                df = df.dropna(subset=['Close'])
                if len(df) < self.rsi_period + 3:
                    continue

                curr = float(df['Close'].iloc[-1])
                ref = self._get_reference_price(df)
                if ref is None or ref <= 0:
                    continue

                drop = (curr - ref) / ref
                rsi_val = float(rsi_wilder(df['Close'], self.rsi_period).iloc[-1])
                if np.isnan(rsi_val):
                    continue

                sma_ok = True
                if self.use_sma_filter and len(df) >= self.sma_period:
                    sma_val = float(sma(df['Close'], self.sma_period).iloc[-1])
                    sma_ok = curr > sma_val if not np.isnan(sma_val) else True

                if drop <= -self.buy_drop and rsi_val < self.rsi_limit and sma_ok:
                    strength = min(1.0, (self.rsi_limit - rsi_val) / self.rsi_limit)
                    signals.append(Signal(
                        symbol=symbol,
                        price=curr,
                        action="BUY",
                        reason=f"DipBuy RSI={rsi_val:.1f} drop={drop*100:.2f}%",
                        strength=strength,
                        indicators={"rsi": rsi_val, "drop": drop, "sma_ok": float(sma_ok)}
                    ))
            except Exception:
                continue

        signals.sort(key=lambda s: s.strength, reverse=True)
        return signals

    def should_exit(self, symbol: str, entry_price: float, current_price: float,
                    holding_bars: int, data: Optional[pd.DataFrame] = None) -> Optional[ExitSignal]:
        if entry_price <= 0:
            return None
        pnl_pct = (current_price - entry_price) / entry_price

        if pnl_pct >= self.sell_gain:
            return ExitSignal(reason="Take Profit", indicators={"pnl_pct": pnl_pct})
        if self.use_stop_loss and pnl_pct <= -self.stop_loss:
            return ExitSignal(reason="Stop Loss", indicators={"pnl_pct": pnl_pct})
        return None


class EnhancedDipBuyStrategy(Strategy):
    """
    Enhanced strategy with:
    - MACD confirmation
    - Bollinger Bands confirmation
    - Volume confirmation
    - Trailing stop
    - Time-based stop
    """

    def __init__(self, params: Dict[str, Any]):
        super().__init__(params)
        self.buy_drop = params.get("buy_drop", 0.02)
        self.sell_gain = params.get("sell_gain", 0.03)
        self.rsi_limit = params.get("rsi_limit", 35)
        self.rsi_period = params.get("rsi_period", 14)
        self.dip_mode = params.get("dip_mode", "DAILY")

        # Stop loss
        self.use_stop_loss = params.get("use_stop_loss", True)
        self.stop_loss = params.get("stop_loss", 0.07)

        # Trailing stop
        self.use_trailing_stop = params.get("use_trailing_stop", True)
        self.trailing_stop_pct = params.get("trailing_stop_pct", 0.02)
        self._high_water_marks: Dict[str, float] = {}

        # Time stop
        self.use_time_stop = params.get("use_time_stop", True)
        self.time_stop_bars = params.get("time_stop_bars", 120)  # ~5 trading days at hourly

        # MACD confirmation
        self.use_macd = params.get("use_macd", True)
        self.macd_fast = params.get("macd_fast", 12)
        self.macd_slow = params.get("macd_slow", 26)
        self.macd_signal = params.get("macd_signal", 9)

        # Bollinger Bands
        self.use_bollinger = params.get("use_bollinger", True)
        self.bb_period = params.get("bb_period", 20)
        self.bb_std = params.get("bb_std", 2.0)

        # Volume
        self.use_volume_filter = params.get("use_volume_filter", False)
        self.volume_multiplier = params.get("volume_multiplier", 1.5)

        # SMA filter
        self.use_sma_filter = params.get("use_sma_filter", False)
        self.sma_period = params.get("sma_period", 200)

    def _get_reference_price(self, df: pd.DataFrame) -> Optional[float]:
        if len(df) < 2:
            return None
        if self.dip_mode == "DAILY":
            last_bar_date = df.index[-1].date() if hasattr(df.index[-1], 'date') else None
            if last_bar_date is not None:
                prev_days = df[df.index.date < last_bar_date]
                if not prev_days.empty:
                    return float(prev_days['Close'].iloc[-1])
            return float(df['Close'].iloc[-2])
        else:
            return float(df['Close'].iloc[-2])

    def generate_signals(self, data: Dict[str, pd.DataFrame], existing_positions: List[str]) -> List[Signal]:
        signals = []
        for symbol, df in data.items():
            if symbol in existing_positions:
                continue
            try:
                df = df.dropna(subset=['Close'])
                if len(df) < max(self.rsi_period + 3, self.macd_slow + 5, self.bb_period + 3):
                    continue

                curr = float(df['Close'].iloc[-1])
                ref = self._get_reference_price(df)
                if ref is None or ref <= 0:
                    continue

                drop = (curr - ref) / ref
                rsi_val = float(rsi_wilder(df['Close'], self.rsi_period).iloc[-1])
                if np.isnan(rsi_val):
                    continue

                # Base conditions
                if drop > -self.buy_drop or rsi_val >= self.rsi_limit:
                    continue

                score = 0.0
                total_weight = 0.0
                indicators = {"rsi": rsi_val, "drop": drop}

                # RSI score (weight 3)
                rsi_score = min(1.0, (self.rsi_limit - rsi_val) / self.rsi_limit)
                score += rsi_score * 3
                total_weight += 3

                # MACD confirmation (weight 2)
                if self.use_macd:
                    macd_line, signal_line, hist = macd(
                        df['Close'], self.macd_fast, self.macd_slow, self.macd_signal
                    )
                    macd_val = float(hist.iloc[-1])
                    macd_prev = float(hist.iloc[-2])
                    indicators["macd_hist"] = macd_val

                    # MACD histogram turning up = bullish divergence
                    if macd_val > macd_prev:
                        score += 1.0 * 2
                    total_weight += 2

                # Bollinger Bands (weight 2)
                if self.use_bollinger:
                    upper, middle, lower, pct_b = bollinger_bands(
                        df['Close'], self.bb_period, self.bb_std
                    )
                    bb_pct = float(pct_b.iloc[-1])
                    indicators["bb_pct_b"] = bb_pct

                    # Price near or below lower band = oversold
                    if bb_pct < 0.2:
                        score += 1.0 * 2
                    elif bb_pct < 0.4:
                        score += 0.5 * 2
                    total_weight += 2

                # Volume confirmation (weight 1)
                if self.use_volume_filter and 'Volume' in df.columns:
                    vol_avg = float(volume_sma(df['Volume'], 20).iloc[-1])
                    vol_curr = float(df['Volume'].iloc[-1])
                    indicators["volume_ratio"] = vol_curr / vol_avg if vol_avg > 0 else 0

                    if vol_curr > vol_avg * self.volume_multiplier:
                        score += 1.0
                    total_weight += 1

                # SMA filter (pass/fail, not scored)
                if self.use_sma_filter and len(df) >= self.sma_period:
                    sma_val = float(sma(df['Close'], self.sma_period).iloc[-1])
                    if not np.isnan(sma_val) and curr <= sma_val:
                        continue  # skip if below SMA
                    indicators["sma_ok"] = 1.0

                strength = score / total_weight if total_weight > 0 else 0
                signals.append(Signal(
                    symbol=symbol,
                    price=curr,
                    action="BUY",
                    reason=f"Enhanced RSI={rsi_val:.1f} drop={drop*100:.2f}% score={strength:.2f}",
                    strength=strength,
                    indicators=indicators
                ))
            except Exception:
                continue

        signals.sort(key=lambda s: s.strength, reverse=True)
        return signals

    def should_exit(self, symbol: str, entry_price: float, current_price: float,
                    holding_bars: int, data: Optional[pd.DataFrame] = None) -> Optional[ExitSignal]:
        if entry_price <= 0:
            return None
        pnl_pct = (current_price - entry_price) / entry_price

        # Update high water mark for trailing stop
        hwm = self._high_water_marks.get(symbol, current_price)
        if current_price > hwm:
            hwm = current_price
            self._high_water_marks[symbol] = hwm

        # Take profit
        if pnl_pct >= self.sell_gain:
            self._high_water_marks.pop(symbol, None)
            return ExitSignal(reason="Take Profit", indicators={"pnl_pct": pnl_pct})

        # Stop loss
        if self.use_stop_loss and pnl_pct <= -self.stop_loss:
            self._high_water_marks.pop(symbol, None)
            return ExitSignal(reason="Stop Loss", indicators={"pnl_pct": pnl_pct})

        # Trailing stop: if price dropped trailing_stop_pct from high water mark
        if self.use_trailing_stop and pnl_pct > 0:
            trail_drop = (current_price - hwm) / hwm
            if trail_drop <= -self.trailing_stop_pct:
                self._high_water_marks.pop(symbol, None)
                return ExitSignal(
                    reason=f"Trailing Stop (from HWM ${hwm:.2f})",
                    indicators={"pnl_pct": pnl_pct, "trail_drop": trail_drop, "hwm": hwm}
                )

        # Time stop
        if self.use_time_stop and holding_bars >= self.time_stop_bars:
            self._high_water_marks.pop(symbol, None)
            return ExitSignal(
                reason=f"Time Stop ({holding_bars} bars)",
                indicators={"pnl_pct": pnl_pct, "holding_bars": holding_bars}
            )

        return None

    def reset(self):
        """Reset internal state (for backtesting between runs)."""
        self._high_water_marks.clear()


# Strategy registry
STRATEGIES = {
    "dip_buy": DipBuyStrategy,
    "enhanced_dip_buy": EnhancedDipBuyStrategy,
}


def create_strategy(name: str, params: Dict[str, Any]) -> Strategy:
    cls = STRATEGIES.get(name)
    if cls is None:
        raise ValueError(f"Unknown strategy: {name}. Available: {list(STRATEGIES.keys())}")
    return cls(params)
