import os, socket, time, sys

host = os.getenv("IB_HOST", "ib-gateway")
port = int(os.getenv("IB_PORT", "4002"))

deadline = time.time() + 180  # 3 min
while time.time() < deadline:
    try:
        s = socket.create_connection((host, port), timeout=2)
        s.close()
        print(f"IB gateway ready at {host}:{port}")
        sys.exit(0)
    except Exception:
        print(f"Waiting for {host}:{port} ...")
        time.sleep(3)

print("Timeout waiting for IB gateway")
sys.exit(1)

