"""Anvil console-script entry point."""

import threading
import time
import webbrowser

import uvicorn

HOST = "127.0.0.1"
PORT = 8000


def _open_browser():
    time.sleep(1)
    webbrowser.open(f"http://{HOST}:{PORT}")


def run():
    threading.Thread(target=_open_browser, daemon=True).start()
    uvicorn.run("anvil.app:app", host=HOST, port=PORT)


if __name__ == "__main__":
    run()
