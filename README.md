# 💻 Web Terminal - Browser-Based Linux PTY Terminal

A sleek, ultra-responsive web-based terminal emulator and system dashboard built with Python `aiohttp`, native Linux pseudo-terminals (`pty`), and `xterm.js`. Enables secure shell access to your Debian server from any web browser or mobile phone without installing native SSH clients.

---

## 🌟 Key Features

- **Full Linux PTY Emulation**:
  - Direct pseudo-terminal allocation via Linux `pty.openpty()` and `fork()`.
  - True interactive shell experience: full ANSI color support, bash auto-completion, `vim`, `htop`, `tmux`, and interactive curses TUIs.
- **WebSocket Streaming & Session Persistence**:
  - Asynchronous full-duplex communication powered by `aiohttp`.
  - Session ring buffer preserves scrollback history across network reconnections.
  - Dynamically propagates browser window and terminal column/row resizing to the kernel via `TIOCSWINSZ` ioctl.
- **Integrated System Hardware Telemetry**:
  - Live sidebar displaying CPU load %, RAM utilization, SoC thermals, and system uptime via `psutil`.

---

## 🛠️ Tech Stack & Requirements

- **Backend:** Python 3.10+ (`aiohttp`, `psutil`)
- **Frontend:** HTML5, CSS3, `xterm.js` (WebSockets)
- **Systemd Unit:** `web-terminal.service`
- **Default Port:** `8088`

### Debian Setup:
```bash
sudo apt update
sudo apt install -y python3 python3-pip

cd web-terminal
pip install aiohttp psutil
```

---

## 🚀 Running the Web Terminal

### Direct Execution:
```bash
PORT=8088 python3 server.py
```
Default binds to `127.0.0.1:8088` (or set `HOST=0.0.0.0` if exposed directly on a trusted local network).
Open in browser: `http://<server-ip>:8088`

### Systemd Background Service on Debian:
```bash
sudo cp web-terminal.service /etc/systemd/system/web-terminal.service
sudo systemctl daemon-reload
sudo systemctl enable --now web-terminal.service
sudo systemctl status web-terminal.service
```
