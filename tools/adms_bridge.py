"""Bridge a ZKTeco ADMS device that cannot speak HTTPS to a Railway deployment.

Some terminals can only push over plain HTTP, or have a CA bundle old enough that
they reject the deployment's certificate. Railway always redirects plain HTTP to
HTTPS — including on ``POST`` — and ADMS firmware generally will not follow that
redirect, so the device appears to push while nothing ever arrives.

This runs on any machine on the **same network** as the device. It listens for
the device on plain HTTP and forwards each request to the deployment over a
properly verified HTTPS connection, returning the reply unchanged. The device
cannot tell the difference.

Run it somewhere always-on in the office — a small PC, a Raspberry Pi, a NAS —
and point the device at that machine's LAN address.

    python tools/adms_bridge.py --upstream https://kenems.up.railway.app

Then on the device:

    Server Mode         : ADMS
    Enable Domain Name  : OFF  (this is what makes the port field visible)
    Server Address      : the bridge machine's LAN IP, e.g. 192.168.1.50
    Server Port         : 8081 (the factory default — leave it alone)
    HTTPS               : OFF
    Comm Key            : the device's six-digit key

Port 8081 is deliberate: it is a common ZKTeco factory default, so the device
usually needs no change, and it is above 1024, so the bridge needs no
administrator rights. If your firmware shows a different default, pass that with
``--port`` instead.

The server-address field must be a real address. Firmware often ships with
``0.0.0.0`` in that field, which means "nowhere" — leaving it is the most common
reason a terminal never connects.

It logs every request it forwards, so you can watch the device arrive:

    [bridge] GET  /iclock/cdata?SN=CO8D... -> 200 (26 bytes)
    [bridge] POST /iclock/cdata?SN=CO8D... -> 200 (2 bytes)

No third-party packages: standard library only, so it runs anywhere Python does.
"""

from __future__ import annotations

import argparse
import ssl
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = ""
VERBOSE = False


class BridgeHandler(BaseHTTPRequestHandler):
    """Forward every request to the upstream deployment, byte for byte."""

    server_version = "ADMSBridge/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: A003 - base class signature
        """Silence the default per-request stderr chatter; we log our own."""
        if VERBOSE:
            sys.stderr.write(f"[bridge] {fmt % args}\n")

    # -- forwarding ------------------------------------------------------
    def _forward(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None

        target = f"{UPSTREAM}{self.path}"
        request = urllib.request.Request(target, data=body, method=method)

        # Forward the content type so form-encoded pushes stay form-encoded.
        content_type = self.headers.get("Content-Type")
        if content_type:
            request.add_header("Content-Type", content_type)
        # The device's own identity headers, when its firmware sends any.
        for header in ("X-Device-SN", "X-Device-Secret", "X-Device-Signature"):
            value = self.headers.get(header)
            if value:
                request.add_header(header, value)
        # A device cannot set this, but some firmware appends it to the query.
        request.add_header("User-Agent", self.headers.get("User-Agent") or "ADMSBridge")

        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                status = response.status
                payload = response.read()
                reply_type = response.headers.get("Content-Type", "text/plain")
        except urllib.error.HTTPError as error:
            # Pass the deployment's own refusal through unchanged: the device
            # must see the same 401/500 it would have seen directly.
            status = error.code
            payload = error.read()
            reply_type = error.headers.get("Content-Type", "text/plain")
        except Exception as error:  # noqa: BLE001
            # Anything else is the bridge's problem, not the device's: answer
            # non-OK so the device holds the record and retries.
            print(f"[bridge] {method} {self.path} -> BRIDGE ERROR {error}", flush=True)
            self._reply(502, b"ERROR", "text/plain")
            return

        self._reply(status, payload, reply_type)
        print(
            f"[bridge] {method:<4} {self.path} -> {status} ({len(payload)} bytes)",
            flush=True,
        )

    def _reply(self, status: int, payload: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        # Devices are simple clients; never let anything cache a reply.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 - base class naming
        self._forward("GET")

    def do_POST(self) -> None:  # noqa: N802 - base class naming
        self._forward("POST")

    def do_PUT(self) -> None:  # noqa: N802 - base class naming
        self._forward("PUT")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--upstream",
        default="https://kenems.up.railway.app",
        help="The deployment to forward to (default: %(default)s)",
    )
    parser.add_argument("--host", default="0.0.0.0", help="Interface to listen on")
    parser.add_argument(
        "--port", type=int, default=8081,
        help=(
            "Port to listen on (default: %(default)s). 8081 is a common ZKTeco "
            "factory default, so the device often needs no change; it is also "
            "above 1024, so no administrator rights are needed. Use 80 only if "
            "the device insists on it, which requires root/Administrator."
        ),
    )
    parser.add_argument("--verbose", action="store_true", help="Log raw request lines")
    args = parser.parse_args(argv)

    global UPSTREAM, VERBOSE
    UPSTREAM = args.upstream.rstrip("/")
    VERBOSE = args.verbose

    # Verify the upstream certificate properly. If this machine trusts the
    # deployment, the bridge will — and the device never has to.
    ssl.create_default_context()

    print("=" * 66)
    print(" ADMS bridge")
    print("=" * 66)
    print(f"  forwarding to : {UPSTREAM}")
    print(f"  listening on  : {args.host}:{args.port}")
    print()
    print("  On the device set:")
    print(f"    Server Address : this machine's LAN IP")
    print(f"    Server Port    : {args.port}")
    print("    HTTPS          : OFF")
    print()
    print("  Press Ctrl+C to stop.")
    print("=" * 66)

    server = ThreadingHTTPServer((args.host, args.port), BridgeHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[bridge] stopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
