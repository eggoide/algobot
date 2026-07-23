#!/usr/bin/env python3
"""Jednorázová likvidace VŠECH otevřených pozic na IB (market order).

Použití (spouštět uvnitř kontejneru 'algobot', kde je ib_insync + síť na ib-gateway):
    docker cp scripts/liquidate_all.py algobot:/tmp/liquidate_all.py
    docker exec algobot python3 /tmp/liquidate_all.py           # DRY-RUN (jen vypíše)
    docker exec algobot python3 /tmp/liquidate_all.py --yes     # reálně prodá

Bezpečnostní pojistky:
  * vlastní clientId (9) — nekoliduje s botem (clientId 2)
  * PAPER guard — abort, pokud účet nezačíná 'DU' (leda s --allow-live)
  * DRY-RUN je default; reálné příkazy jen s --yes
  * long pozice se zavírají SELL, short pozice BUY (obecně)
"""
import os
import sys
import time

from ib_insync import IB, Stock, MarketOrder

IB_HOST = os.getenv("IB_HOST", "ib-gateway")
IB_PORT = int(os.getenv("IB_PORT", "4002"))
CLIENT_ID = 13  # záměrně jiný než bot (2)
FILL_TIMEOUT_SEC = 45


def main() -> int:
    assume_yes = "--yes" in sys.argv or "-y" in sys.argv
    allow_live = "--allow-live" in sys.argv

    ib = IB()
    print(f"Připojuji k IB {IB_HOST}:{IB_PORT} (clientId={CLIENT_ID})…")
    ib.connect(IB_HOST, IB_PORT, clientId=CLIENT_ID, timeout=20)
    try:
        accounts = ib.managedAccounts()
        acct = accounts[0] if accounts else "?"
        is_paper = acct.startswith("DU")
        print(f"Účet: {acct} ({'PAPER' if is_paper else 'LIVE'})")
        if not is_paper and not allow_live:
            print("ABORT: účet nevypadá jako paper (DU*). Pro LIVE spusť s --allow-live.", file=sys.stderr)
            return 2

        ib.sleep(0.3)
        positions = [p for p in ib.positions() if p.position != 0]
        if not positions:
            print("Žádné otevřené pozice — není co likvidovat.")
            return 0

        print(f"\nOtevřené pozice ({len(positions)}):")
        plan = []
        for p in positions:
            sym = p.contract.symbol
            qty = p.position
            action = "SELL" if qty > 0 else "BUY"
            abs_qty = int(abs(qty))
            plan.append((sym, action, abs_qty, p.avgCost))
            print(f"  {sym:6s} {qty:+.0f} @ avg {p.avgCost:.2f}  ->  {action} {abs_qty}")

        if not assume_yes:
            print("\nDRY-RUN (bez --yes). Nic neodesláno.")
            return 0

        print("\nOdesílám market příkazy…")
        trades = []
        for sym, action, abs_qty, _ in plan:
            # Bez qualifyContracts — bot.py zadává příkazy stejně přímo a qualify
            # se u SMART/USD při otevření trhu umí zaseknout bez timeoutu.
            contract = Stock(sym, "SMART", "USD")
            order = MarketOrder(action, abs_qty, tif="DAY")
            trades.append((sym, ib.placeOrder(contract, order)))
            print(f"  -> {action} {abs_qty} {sym} odesláno")

        print(f"\nČekám na filly (max {FILL_TIMEOUT_SEC}s)…")
        deadline = time.time() + FILL_TIMEOUT_SEC
        while time.time() < deadline:
            ib.sleep(1.0)
            if all(t.orderStatus.status in ("Filled", "Cancelled", "Inactive", "ApiCancelled")
                   for _, t in trades):
                break

        print("\nVýsledek:")
        all_filled = True
        for sym, t in trades:
            st = t.orderStatus.status
            filled = t.orderStatus.filled
            avg = t.orderStatus.avgFillPrice
            if st != "Filled":
                all_filled = False
            print(f"  {sym:6s} {st:12s} filled {filled:.0f} @ {avg:.2f}")

        if not all_filled:
            print("\nUPOZORNĚNÍ: ne všechny příkazy jsou 'Filled'. Zkontroluj IB / zkus po otevření trhu.",
                  file=sys.stderr)
            return 1
        print("\nHotovo — všechny pozice zavřeny.")
        return 0
    finally:
        ib.disconnect()


if __name__ == "__main__":
    sys.exit(main())
