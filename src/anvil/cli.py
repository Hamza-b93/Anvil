"""Anvil console-script entry point."""

import threading
import time
import webbrowser
from typing import Optional

import uvicorn

HOST = "127.0.0.1"
DEFAULT_PORT = 8000


def _open_browser(port: int):
    time.sleep(1)
    webbrowser.open(f"http://{HOST}:{port}")


def run():
    # Prompt the user for a port number
    try:
        user_input = input(f"Enter the port number to run the server on (default {DEFAULT_PORT}): ")
        if user_input.strip() == "":
            port = DEFAULT_PORT
        else:
            port = int(user_input)
            if not (1 <= port <= 65535):
                print(f"Invalid port number: {port}. Using default port {DEFAULT_PORT}.")
                port = DEFAULT_PORT
    except ValueError:
        print(f"Invalid input. Using default port {DEFAULT_PORT}.")
        port = DEFAULT_PORT
    
    threading.Thread(target=_open_browser, args=(port,), daemon=True).start()
    uvicorn.run("anvil.app:app", host=HOST, port=port)


if __name__ == "__main__":
    run()