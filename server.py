#!/usr/bin/env python3
import os
import sys
import pty
import fcntl
import termios
import struct
import signal
import json
import asyncio
import time
import warnings
from collections import deque
warnings.filterwarnings("ignore", category=DeprecationWarning)
import psutil
import base64
from aiohttp import web

PORT = int(os.getenv("PORT", 8088))
HOST = os.getenv("HOST", "127.0.0.1")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

# Map of persistent sessions: session_id -> {master_fd, pid, ws, buffer, buffer_size, max_buffer_size, created_at, last_active}
sessions = {}

def get_system_stats():
    try:
        cpu_pct = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory()
        uptime_sec = 0
        
        try:
            with open("/proc/uptime", "r") as f:
                uptime_sec = int(float(f.readline().split()[0]))
        except Exception:
            pass

        uptime_str = f"{uptime_sec // 3600}h {(uptime_sec % 3600) // 60}m"

        battery_pct = None
        charging = False
        bat_path = "/sys/class/power_supply/battery"
        if os.path.exists(bat_path):
            try:
                with open(os.path.join(bat_path, "capacity")) as f:
                    battery_pct = int(f.read().strip())
                with open(os.path.join(bat_path, "status")) as f:
                    charging = "Charging" in f.read()
            except Exception:
                pass

        ips = {}
        for iface, addrs in psutil.net_if_addrs().items():
            for addr in addrs:
                if addr.family.name == 'AF_INET' and not addr.address.startswith("127."):
                    ips[iface] = addr.address

        return {
            "cpu_percent": cpu_pct,
            "memory": {
                "used_mb": round(mem.used / (1024 * 1024)),
                "total_mb": round(mem.total / (1024 * 1024)),
                "percent": mem.percent
            },
            "uptime": uptime_str,
            "battery": {
                "percent": battery_pct,
                "charging": charging
            },
            "ips": ips,
            "active_sessions": len(sessions)
        }
    except Exception as e:
        return {"error": str(e)}

def cleanup_session(session_id):
    session = sessions.pop(session_id, None)
    if not session:
        return
    master_fd = session.get("master_fd")
    pid = session.get("pid")
    sockets = session.get("sockets", set())

    try:
        loop = asyncio.get_running_loop()
        if master_fd is not None:
            loop.remove_reader(master_fd)
    except Exception:
        pass

    if master_fd is not None:
        try:
            os.close(master_fd)
        except OSError:
            pass

    if pid is not None:
        try:
            os.kill(pid, signal.SIGHUP)
        except OSError:
            pass
        try:
            os.waitpid(pid, os.WNOHANG)
        except OSError:
            pass

    for ws in list(sockets):
        if not ws.closed:
            try:
                asyncio.ensure_future(ws.close())
            except Exception:
                pass

def create_session(session_id, cols=100, rows=30):
    master_fd, slave_fd = pty.openpty()
    fcntl.fcntl(master_fd, fcntl.F_SETFL, os.O_NONBLOCK)
    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    env = dict(os.environ)
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    env["HOME"] = "/home/droidian"
    env["USER"] = "droidian"
    env["PATH"] = f"/home/droidian/.gemini/antigravity-cli/bin:/home/droidian/.local/bin:{env.get('PATH', '/usr/local/bin:/usr/bin:/bin')}"

    pid = os.fork()
    if pid == 0:
        os.close(master_fd)
        os.login_tty(slave_fd)
        os.chdir("/home/droidian")
        os.execlpe("/bin/bash", "bash", "-l", env)

    os.close(slave_fd)
    loop = asyncio.get_running_loop()

    session_data = {
        "session_id": session_id,
        "master_fd": master_fd,
        "pid": pid,
        "sockets": set(),
        "cols": cols,
        "rows": rows,
        "buffer": deque(),
        "buffer_size": 0,
        "max_buffer_size": 512 * 1024,  # 512 KB scrollback ring buffer
        "created_at": time.time(),
        "last_active": time.time()
    }

    def on_pty_read():
        try:
            data = os.read(master_fd, 8192)
            if not data:
                # EOF: Process exited
                cleanup_session(session_id)
                return

            # Append to persistent scrollback buffer
            session_data["buffer"].append(data)
            session_data["buffer_size"] += len(data)
            while session_data["buffer_size"] > session_data["max_buffer_size"] and session_data["buffer"]:
                popped = session_data["buffer"].popleft()
                session_data["buffer_size"] -= len(popped)
            session_data["last_active"] = time.time()

            # Stream to all connected browsers simultaneously
            for client_ws in list(session_data.get("sockets", set())):
                if not client_ws.closed:
                    async def _send(t_ws=client_ws, b_data=data):
                        try:
                            await t_ws.send_bytes(b_data)
                        except Exception:
                            pass
                    asyncio.create_task(_send())
                else:
                    session_data["sockets"].discard(client_ws)
        except (BlockingIOError, InterruptedError):
            pass
        except OSError:
            # EIO on slave terminal exit
            cleanup_session(session_id)

    loop.add_reader(master_fd, on_pty_read)
    sessions[session_id] = session_data
    return session_data

async def api_system(request):
    return web.json_response(get_system_stats())

async def api_sessions_list(request):
    data = []
    for sid, s in sessions.items():
        active_clients = len([w for w in s.get("sockets", set()) if not w.closed])
        data.append({
            "id": sid,
            "pid": s.get("pid"),
            "connected": active_clients > 0,
            "connected_clients": active_clients,
            "created_at": s.get("created_at"),
            "last_active": s.get("last_active"),
            "buffer_bytes": s.get("buffer_size", 0)
        })
    return web.json_response({"sessions": data})

async def api_sessions_kill(request):
    try:
        body = await request.json()
        sid = body.get("session")
        if sid and sid in sessions:
            cleanup_session(sid)
            return web.json_response({"success": True, "killed": sid})
        return web.json_response({"error": "Session not found"}, status=404)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def websocket_handler(request):
    ws = web.WebSocketResponse(heartbeat=15.0)
    await ws.prepare(request)

    session_id = request.query.get("session", "session-1")
    cols = int(request.query.get("cols", 100))
    rows = int(request.query.get("rows", 30))

    if session_id in sessions:
        # Attach to existing persistent session (multi-client broadcast)
        session = sessions[session_id]
        if "sockets" not in session:
            session["sockets"] = set()
        session["sockets"].add(ws)
        
        # Apply updated window size only if session is new or columns changed (avoid firing SIGWINCH on simple reconnects)
        if cols > 0 and rows > 0:
            existing_cols = session.get("cols")
            if existing_cols is None or cols != existing_cols:
                session["cols"] = cols
                session["rows"] = rows
                try:
                    fcntl.ioctl(session["master_fd"], termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
                except OSError:
                    pass

        # Replay entire scrollback buffer in chunks to rehydrate the newly attached client
        if session["buffer"]:
            all_bytes = b"".join(session["buffer"])
            chunk_size = 32768
            for i in range(0, len(all_bytes), chunk_size):
                if ws.closed:
                    break
                await ws.send_bytes(all_bytes[i:i + chunk_size])
        if not ws.closed:
            await ws.send_str(json.dumps({"type": "replay_done"}))
    else:
        # Create brand new persistent session
        session = create_session(session_id, cols, rows)
        session["sockets"].add(ws)
        await ws.send_str(json.dumps({"type": "replay_done"}))

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.BINARY:
                try:
                    os.write(session["master_fd"], msg.data)
                except OSError:
                    pass
            elif msg.type == web.WSMsgType.TEXT:
                text = msg.data
                if text.startswith('{"type":') and text.endswith("}"):
                    try:
                        cmd = json.loads(text)
                        if cmd.get("type") == "resize":
                            new_cols = int(cmd.get("cols", 80))
                            new_rows = int(cmd.get("rows", 24))
                            if new_cols > 0 and new_rows > 0:
                                suppress_winch = cmd.get("suppress_winch", False)
                                if not suppress_winch:
                                    if new_cols != session.get("cols") or new_rows != session.get("rows"):
                                        session["cols"] = new_cols
                                        session["rows"] = new_rows
                                        try:
                                            fcntl.ioctl(session["master_fd"], termios.TIOCSWINSZ, struct.pack("HHHH", new_rows, new_cols, 0, 0))
                                        except OSError:
                                            pass
                                else:
                                    session["cols"] = new_cols
                                    session["rows"] = new_rows
                            continue
                        elif cmd.get("type") == "kill_session":
                            cleanup_session(session_id)
                            break
                    except Exception:
                        pass
                try:
                    os.write(session["master_fd"], text.encode('utf-8'))
                except OSError:
                    pass
    finally:
        # Browser disconnected: Detach this specific websocket connection only!
        # Do NOT kill the session, do NOT disconnect other browsers!
        if session_id in sessions and "sockets" in sessions[session_id]:
            sessions[session_id]["sockets"].discard(ws)

    return ws

async def index_handler(request):
    index_path = os.path.join(STATIC_DIR, "index.html")
    return web.FileResponse(index_path)

UPLOAD_DIR = "/home/droidian/uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

async def api_upload(request):
    try:
        content_type = request.content_type or ""
        if "application/json" in content_type:
            data = await request.json()
            filename = (data.get("filename") or f"upload_{int(time.time())}").strip()
            filename = os.path.basename(filename)
            filename = "".join(c for c in filename if c.isalnum() or c in "._- ")
            if not filename:
                filename = f"upload_{int(time.time())}"
            base, ext = os.path.splitext(filename)
            target_path = os.path.join(UPLOAD_DIR, filename)
            counter = 1
            while os.path.exists(target_path):
                filename = f"{base}_{counter}{ext}"
                target_path = os.path.join(UPLOAD_DIR, filename)
                counter += 1
            
            raw_data = data.get("data")
            if raw_data and "base64," in raw_data:
                b64 = raw_data.split("base64,")[1]
                content = base64.b64decode(b64)
            elif "content" in data:
                content = data["content"].encode("utf-8")
            else:
                return web.json_response({"error": "No content provided"}, status=400)
            
            with open(target_path, "wb") as f:
                f.write(content)
            
            is_image = ext.lower() in [".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"]
            return web.json_response({
                "success": True,
                "filename": filename,
                "path": target_path,
                "size": len(content),
                "is_image": is_image,
                "url": f"/api/uploads/{filename}"
            })
        elif "multipart" in content_type:
            reader = await request.multipart()
            field = await reader.next()
            if not field:
                return web.json_response({"error": "No file uploaded"}, status=400)
            filename = field.filename or f"upload_{int(time.time())}"
            filename = os.path.basename(filename)
            filename = "".join(c for c in filename if c.isalnum() or c in "._- ")
            if not filename:
                filename = f"upload_{int(time.time())}"
            base, ext = os.path.splitext(filename)
            target_path = os.path.join(UPLOAD_DIR, filename)
            counter = 1
            while os.path.exists(target_path):
                filename = f"{base}_{counter}{ext}"
                target_path = os.path.join(UPLOAD_DIR, filename)
                counter += 1
            size = 0
            with open(target_path, "wb") as f:
                while True:
                    chunk = await field.read_chunk()
                    if not chunk:
                        break
                    size += len(chunk)
                    f.write(chunk)
            is_image = ext.lower() in [".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"]
            return web.json_response({
                "success": True,
                "filename": filename,
                "path": target_path,
                "size": size,
                "is_image": is_image,
                "url": f"/api/uploads/{filename}"
            })
        else:
            return web.json_response({"error": "Unsupported Content-Type"}, status=400)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def api_uploads_list(request):
    try:
        if not os.path.exists(UPLOAD_DIR):
            return web.json_response({"uploads": []})
        files = [f for f in os.listdir(UPLOAD_DIR) if not f.startswith(".")]
        results = []
        for f in files:
            fp = os.path.join(UPLOAD_DIR, f)
            try:
                st = os.stat(fp)
                ext = os.path.splitext(f)[1].lower()
                results.append({
                    "filename": f,
                    "path": fp,
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                    "is_image": ext in [".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"],
                    "url": f"/api/uploads/{f}"
                })
            except Exception:
                pass
        results.sort(key=lambda x: x["mtime"], reverse=True)
        return web.json_response({"uploads": results[:10]})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def api_upload_serve(request):
    raw_name = request.match_info.get("filename", "")
    safe_name = os.path.basename(raw_name)
    fp = os.path.join(UPLOAD_DIR, safe_name)
    if os.path.exists(fp) and os.path.isfile(fp):
        return web.FileResponse(fp)
    return web.Response(text="File not found", status=404)

async def on_cleanup(app):
    for sid in list(sessions.keys()):
        cleanup_session(sid)

def create_app():
    app = web.Application()
    app.router.add_get("/", index_handler)
    app.router.add_get("/api/system", api_system)
    app.router.add_get("/api/sessions", api_sessions_list)
    app.router.add_post("/api/sessions/kill", api_sessions_kill)
    app.router.add_post("/api/upload", api_upload)
    app.router.add_get("/api/uploads", api_uploads_list)
    app.router.add_get("/api/uploads/{filename}", api_upload_serve)
    app.router.add_get("/ws", websocket_handler)
    app.router.add_static("/static/", path=STATIC_DIR, name="static")
    app.on_cleanup.append(on_cleanup)
    return app

if __name__ == "__main__":
    app = create_app()
    print(f"⚡ [Web Terminal] Starting on http://{HOST}:{PORT}")
    web.run_app(app, host=HOST, port=PORT, access_log=None)
