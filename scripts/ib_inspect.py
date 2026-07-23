#!/usr/bin/env python3
"""Read-only inspekce IB stavu (pozice + otevřené příkazy). Vlastní clientId 11."""
import os, sys
from ib_insync import IB

ib = IB()
ib.connect(os.getenv("IB_HOST", "ib-gateway"), int(os.getenv("IB_PORT", "4002")),
           clientId=11, timeout=20)
try:
    accounts = ib.managedAccounts()
    print("Účet:", accounts[0] if accounts else "?")
    ib.sleep(0.5)
    pos = [p for p in ib.positions() if p.position != 0]
    print(f"\nPOZICE ({len(pos)}):")
    for p in pos:
        print(f"  {p.contract.symbol:6s} {p.position:+.0f} @ avg {p.avgCost:.2f}")
    if not pos:
        print("  (žádné — účet flat)")
    ib.reqAllOpenOrders()
    ib.sleep(1.0)
    trades = ib.openTrades()
    print(f"\nOTEVŘENÉ / AKTIVNÍ PŘÍKAZY ({len(trades)}):")
    for t in trades:
        os_ = t.orderStatus
        print(f"  {t.contract.symbol:6s} {t.order.action} {t.order.totalQuantity:.0f} "
              f"[{os_.status}] filled {os_.filled:.0f} @ {os_.avgFillPrice or 0:.2f} "
              f"remaining {os_.remaining:.0f}")
    if not trades:
        print("  (žádné aktivní příkazy)")
finally:
    ib.disconnect()
