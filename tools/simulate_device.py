"""Simulate a ZKTeco ADMS terminal against a live deployment.

Use this to separate two very different failures:

* **Server problem** — this script fails too, so the endpoint is at fault.
* **Device problem** — this script succeeds, so the endpoint is fine and the
  terminal's own configuration is wrong.

It performs the same call sequence the hardware does, in the same order, with
the same content type and the same form fields.

Usage::

    python tools/simulate_device.py <base-url> <serial> <CommKey>
    python tools/simulate_device.py <base-url> <serial> <CommKey> --read-only

**The push in step 3 writes a real record** to whatever database you point this
at. Against production that means a real punch from a fake device, which will
appear in the live stream and against whichever employee maps to device user 1.
Use ``--read-only`` to run only the checks that write nothing (reachability,
handshake, command poll), or clean up afterwards with::

    flask --app wsgi shell
    >>> from app.features.biometrics.models import BiometricRawLog
    >>> BiometricRawLog.query.filter_by(device_user_id="1").delete()

The CommKey is the secret shown once when the device was registered. If it was
lost, rotate it in the app and use the new one.
"""

from __future__ import annotations

import sys
from urllib.parse import urlencode

try:
    import urllib.error
    import urllib.request
except ImportError:  # pragma: no cover
    raise SystemExit("Python 3 is required.")


def call(url: str, *, method: str = "GET", form: dict | None = None) -> tuple[int, str]:
    """Make one request the way a device would."""
    data = urlencode(form).encode() if form else None
    request = urllib.request.Request(url, data=data, method=method)
    if data:
        request.add_header(
            "Content-Type", "application/x-www-form-urlencoded"
        )
    # Devices send a minimal agent and accept anything.
    request.add_header("Accept", "*/*")
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")
    except Exception as error:  # noqa: BLE001
        return 0, f"{type(error).__name__}: {error}"


def main(argv: list[str]) -> int:
    args = [item for item in argv[1:] if not item.startswith("--")]
    read_only = any(item == "--read-only" for item in argv)

    if len(args) < 3:
        print(__doc__)
        return 2

    base = args[0].rstrip("/")
    serial = args[1].strip()
    comm_key = args[2].strip()

    print(f"target : {base}")
    print(f"serial : {serial!r}")
    print(f"commkey: {comm_key[:4]}... ({len(comm_key)} chars)")
    if read_only:
        print("mode   : read-only (nothing will be written)")
    else:
        print("mode   : full — step 3 WRITES a real punch to this database")
    print()

    failures: list[str] = []

    # --- 1. reachability ---------------------------------------------------
    print("=== 1. is the endpoint reachable at all? ===")
    status, body = call(f"{base}/iclock/ping")
    ok = status == 200 and body.strip() == "OK"
    print(f"  [{'ok  ' if ok else 'FAIL'}] GET /iclock/ping -> {status} {body.strip()[:60]!r}")
    if not ok:
        failures.append("the endpoint is not reachable")
        print("\n  The server never answered. Check the domain, and that the")
        print("  deployment is running before debugging the device.")
        return 1

    # --- 2. handshake ------------------------------------------------------
    print("\n=== 2. handshake (what the device sends first) ===")
    query = urlencode({"SN": serial, "options": "all", "CommKey": comm_key})
    status, body = call(f"{base}/iclock/cdata?{query}")
    handshake_ok = status == 200 and "GET OPTION FROM" in body
    print(f"  [{'ok  ' if handshake_ok else 'FAIL'}] GET /iclock/cdata -> {status}")
    for line in body.strip().splitlines()[:6]:
        print(f"        {line}")
    if status == 401:
        print("  401 -> the serial or the CommKey was rejected.")
        print("        The serial must match the registered device EXACTLY")
        print("        (it is case-sensitive), and the CommKey must be its secret.")
        failures.append("handshake rejected")
    elif not handshake_ok:
        failures.append(f"handshake returned {status}")
    else:
        print("  The device would now start pushing records.")

    # --- 3. push an attendance record -------------------------------------
    print("\n=== 3. push one ATTLOG record (what the device does next) ===")
    if read_only:
        print("  skipped (--read-only): this step would write a real punch")
    else:
        form = {
            "SN": serial,
            "table": "ATTLOG",
            "Stamp": "9999",
            "CommKey": comm_key,
            # PIN <tab> DateTime <tab> Status <tab> Verify <tab> WorkCode
            "data": "1\t2026-10-16 08:12:33\t0\t1\t0\n",
        }
        status, body = call(f"{base}/iclock/cdata", method="POST", form=form)
        push_ok = body.strip() == "OK"
        print(f"  [{'ok  ' if push_ok else 'FAIL'}] POST /iclock/cdata -> {status} {body.strip()[:60]!r}")
        if push_ok:
            print("  Accepted. A REAL punch is now in biometric_raw_logs for")
            print("  device user 1. Remove it once satisfied — see this file's")
            print("  docstring for the cleanup snippet.")
        else:
            failures.append(f"push returned {status}")

    # --- 4. command polling ------------------------------------------------
    print("\n=== 4. command poll (the device's idle loop) ===")
    status, body = call(
        f"{base}/iclock/getrequest?{urlencode({'SN': serial, 'CommKey': comm_key})}"
    )
    poll_ok = status == 200 and body.strip() == "OK"
    print(f"  [{'ok  ' if poll_ok else 'FAIL'}] GET /iclock/getrequest -> {status} {body.strip()[:40]!r}")
    if not poll_ok:
        failures.append(f"getrequest returned {status}")

    # --- verdict -----------------------------------------------------------
    print("\n" + "=" * 68)
    if failures:
        print(f"RESULT: {len(failures)} problem(s) — the SERVER refused something")
        for failure in failures:
            print("  -", failure)
        print("\nSince this script failed, the problem is on the server side or in")
        print("the credentials you passed. The device is probably fine.")
    else:
        print("RESULT: all calls succeeded")
        print("\nThe endpoint works with these exact credentials. If the device")
        print("still shows offline, the difference is in the DEVICE's own setup —")
        print("its serial number or CommKey differs from what you passed here,")
        print("or it is not actually reaching the internet.")
    print("=" * 68)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
