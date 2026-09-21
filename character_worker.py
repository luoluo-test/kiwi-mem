"""Private worker entry point. Never expose its loopback port to clients."""
import os
import threading
import time


def watch_supervisor():
    import character_boundary
    while True:
        time.sleep(5)
        if time.monotonic() - character_boundary.last_supervisor_contact > 45:
            # A killed supervisor must not leave orphan schedulers writing forever.
            os._exit(70)

if __name__ == "__main__":
    if not os.getenv("KIWI_WORKER_TOKEN") or not os.getenv("KIWI_CHARACTER_ID"):
        raise SystemExit("Start through character_gateway.py")
    import uvicorn
    from main import app
    threading.Thread(target=watch_supervisor, daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ["PORT"]), access_log=False)
