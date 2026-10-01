#!/usr/bin/env python3
"""Reverse SSH tunnel for jarvis-serve.

The agent VM cannot accept inbound tailnet connections (client-only
Tailscale enrollment), so OpenCode on the user's PC cannot dial the
jarvis-serve server directly. This script opens a reverse forward over the
existing SSH path to the PC:

    PC 127.0.0.1:<remote_port>  ──SSH──▶  VM 127.0.0.1:<local_port>

OpenCode on the PC then uses baseURL http://127.0.0.1:<remote_port>/v1.

Reuses the win-ssh CONNECT-through-proxy pattern (runtime HTTP proxy,
port 3130 selects Tailscale). Auto-reconnects with backoff; logs to
logs/reverse-tunnel.log. Stop with SIGTERM/SIGINT.

Usage: python3 scripts/reverse-tunnel.py [--local-port 8765]
       [--remote-port 18765] [--remote-host 100.110.92.50]
"""
import argparse, base64, logging, os, select, socket, sys, threading, time, urllib.parse
import paramiko  # run with ~/workspace/ssh-tunnel/.venv/bin/python

logging.basicConfig(
    filename=os.path.expanduser("~/workspace/jarvis-serve/logs/reverse-tunnel.log"),
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

REMOTE_USER = os.environ.get("JARVIS_SSH_USER", "91952")
KEY = os.path.expanduser(os.environ.get("JARVIS_SSH_KEY", "~/.ssh/id_bhavya_win"))
KNOWN = os.path.expanduser(os.environ.get("JARVIS_SSH_KNOWN_HOSTS", "~/.ssh/known_hosts_tailscale"))

def connect_tunnel(target_host, target_port=22, timeout=30):
    proxy_url = os.environ["HTTPS_PROXY"]
    p = urllib.parse.urlparse(proxy_url)
    proxy_host, proxy_port = p.hostname, 3130
    user, pw = (p.username or ""), (p.password or "")
    auth = ""
    if user:
        auth = "Proxy-Authorization: Basic " + base64.b64encode(
            f"{user}:{pw}".encode()).decode() + "\r\n"
    sock = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    req = (f"CONNECT {target_host}:{target_port} HTTP/1.1\r\n"
           f"Host: {target_host}:{target_port}\r\n{auth}\r\n")
    sock.sendall(req.encode())
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("proxy closed connection during CONNECT")
        data += chunk
    status = data.split(b"\r\n", 1)[0].decode()
    if " 200" not in status:
        raise RuntimeError(f"CONNECT failed: {status}")
    return sock

def bridge(chan, target_host, target_port):
    """Bidirectional forward between an SSH channel and a local socket."""
    try:
        downstream = socket.create_connection((target_host, target_port), timeout=10)
    except OSError:
        chan.close()
        return
    chan.settimeout(0.0)
    downstream.setblocking(False)
    try:
        while True:
            r, _, _ = select.select([chan, downstream], [], [], 60)
            if not r:
                continue
            if chan in r:
                try:
                    data = chan.recv(32768)
                except Exception:
                    break
                if not data:
                    break
                downstream.sendall(data)
            if downstream in r:
                try:
                    data = downstream.recv(32768)
                except BlockingIOError:
                    data = b""
                if not data:
                    break
                chan.sendall(data)
    finally:
        chan.close()
        downstream.close()

def serve_forever(args):
    backoff = 5
    while True:
        try:
            logging.info("dialing %s:22 via proxy", args.remote_host)
            sock = connect_tunnel(args.remote_host, 22)
            t = paramiko.Transport(sock)
            t.set_keepalive(30)
            try:
                saved = {}
                if os.path.exists(KNOWN):
                    with open(KNOWN) as f:
                        for line in f:
                            parts = line.split()
                            if len(parts) >= 3:
                                saved[(parts[0], parts[1])] = parts[2]
                t.start_client(timeout=30)
                remote_key = t.get_remote_server_key()
                ktype, kb64 = remote_key.get_name(), remote_key.get_base64()
                pinned = saved.get((args.remote_host, ktype))
                if pinned is None:
                    with open(KNOWN, "a") as f:
                        f.write(f"{args.remote_host} {ktype} {kb64}\n")
                    logging.info("pinned new host key for %s", args.remote_host)
                elif pinned != kb64:
                    raise RuntimeError(
                        f"HOST KEY CHANGED for {args.remote_host} - refusing")
                t.auth_publickey(args.ssh_user,
                                 paramiko.Ed25519Key.from_private_key_file(args.ssh_key))
            except Exception:
                t.close()
                raise
            bound = t.request_port_forward("127.0.0.1", args.remote_port)
            logging.info("reverse forward up: PC 127.0.0.1:%d -> VM 127.0.0.1:%d (sshd reports %s)",
                         args.remote_port, args.local_port, bound)
            backoff = 5
            while t.is_active():
                chan = t.accept(timeout=20)
                if chan is None:
                    continue
                peer = chan.origin_addr if hasattr(chan, "origin_addr") else "?"
                logging.info("incoming forwarded connection from %s", peer)
                threading.Thread(target=bridge,
                                 args=(chan, "127.0.0.1", args.local_port),
                                 daemon=True).start()
            logging.warning("transport went inactive; reconnecting")
        except Exception as e:
            logging.warning("tunnel error: %s; retry in %ss", e, backoff)
        time.sleep(backoff)
        backoff = min(backoff * 2, 120)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--local-port", type=int, default=8765)
    ap.add_argument("--remote-port", type=int, default=18765)
    ap.add_argument("--remote-host", default=os.environ.get("JARVIS_PC_HOST", "100.110.92.50"))
    ap.add_argument("--ssh-user", default=REMOTE_USER)
    ap.add_argument("--ssh-key", default=KEY)
    serve_forever(ap.parse_args())
