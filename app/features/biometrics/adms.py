"""ZKTeco ADMS ("iClock") push-protocol parsing.

Stock ZKTeco terminals — including the MB20-VL, whose specification lists ADMS as
a standard function — can be pointed at a plain HTTP server. They then push their
records there. That protocol is what this module understands.

It is deliberately *not* the same as the JSON webhook in
:mod:`app.features.biometrics.blueprint`. The differences matter, because a real
device cannot do what the JSON endpoint expects:

* The device posts ``application/x-www-form-urlencoded``, not JSON. Its body is
  ``SN=...&table=ATTLOG&Stamp=...&data=...``, not an object.
* It cannot set custom headers. The device menu offers only a Server Address, a
  Server Port and a communication key, so ``X-Device-SN`` /
  ``X-Device-Secret`` are impossible for it to send. Its serial number arrives in
  the **body** instead, and the key arrives as ``CommKey``.
* It expects a **plain-text** reply. ``OK`` means stored; anything else makes the
  device retry, which is exactly the behaviour wanted on a failure.

Everything here is pure: no database, no Flask, no clock. That keeps the wire
format testable against captured payloads, which is the only way to be confident
about a protocol whose peer is a piece of hardware.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

#: The attendance table name in a ``table=`` push.
TABLE_ATTLOG = "ATTLOG"

#: Table names a device may use for its own description / capability report.
TABLE_OPERLOG = "OPERLOG"
TABLE_OPTIONS = "OPTIONS"

#: The reply that tells a device its data was accepted. Anything else is treated
#: as a failure and the record is retried, so this must only be sent on success.
REPLY_OK = "OK"

#: Accepted timestamp layouts. ZKTeco firmware is inconsistent about seconds and
#: about the date/time separator, so all the observed forms are listed.
_TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%d",
)


@dataclass(frozen=True)
class ADMSPunch:
    """One attendance line, normalised."""

    device_user_id: str
    punch_time: datetime
    #: Raw status digit as sent by the device (0 in, 1 out, and other states).
    status: str
    verify_mode: str | None = None
    work_code: str | None = None
    #: The untouched line, kept so nothing is lost if parsing rules change.
    raw_line: str = ""


@dataclass
class ADMSPush:
    """A parsed device push."""

    serial_no: str
    table: str
    stamp: str | None = None
    comm_key: str | None = None
    punches: list[ADMSPunch] = field(default_factory=list)
    #: ``~SerialNumber=`` / ``~DeviceName=`` style lines from the body.
    device_info: dict[str, str] = field(default_factory=dict)
    #: Lines that were not understood; reported rather than silently dropped.
    unparsed: list[str] = field(default_factory=list)


def parse_time(value: str) -> datetime | None:
    """Parse a device timestamp, or return ``None``.

    Device clocks are frequently wrong but the format is stable; an unparseable
    value means the line is not a punch rather than that the clock is odd.
    """
    text = (value or "").strip()
    if not text:
        return None
    for layout in _TIME_FORMATS:
        try:
            return datetime.strptime(text, layout)
        except ValueError:
            continue
    return None


#: Matches a leading clock fragment, used to rejoin a space-split timestamp.
_TIME_PART = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")


def parse_punch_line(line: str) -> ADMSPunch | None:
    """Parse one ``ATTLOG`` row.

    The row is tab-separated::

        PIN  DateTime  Status  Verify  WorkCode  Reserved...

    Firmware varies in how many trailing columns it sends, so only the first
    three are required and the rest are optional.

    Returns ``None`` for anything that is not a punch. That matters: a line the
    parser does not understand is reported to the caller and the batch is
    retried, whereas a line it *wrongly* accepts becomes a bogus attendance
    record. So the checks below are strict about shape.
    """
    raw = line.rstrip("\r\n")
    if not raw.strip():
        return None

    # Tabs are the documented separator; some builds use spaces instead. Pick
    # the separator by looking for tabs, because a timestamp contains a space
    # and splitting on whitespace would tear "2026-10-16 09:30:00" in two.
    if "\t" in raw:
        parts = [piece.strip() for piece in raw.split("\t")]
    else:
        parts = raw.split()
        # Rejoin the timestamp that the split just broke apart.
        if len(parts) >= 4 and _TIME_PART.match(parts[2] or ""):
            parts = [parts[0], f"{parts[1]} {parts[2]}"] + parts[3:]

    if len(parts) < 3:
        return None

    user_id, raw_time, status = parts[0], parts[1], parts[2]
    if not user_id:
        return None
    if not status.isdigit():
        # Guards against a shifted row, e.g. "PIN<tab>DateTime" with the status
        # column absent: the time would otherwise be read as a status.
        return None

    punch_time = parse_time(raw_time)
    if punch_time is None:
        return None

    return ADMSPunch(
        device_user_id=user_id,
        punch_time=punch_time,
        status=status,
        verify_mode=parts[3] if len(parts) > 3 and parts[3] else None,
        work_code=parts[4] if len(parts) > 4 and parts[4] else None,
        raw_line=raw,
    )


def parse_cdata(
    form: dict[str, str],
    *,
    raw_body: str = "",
) -> ADMSPush:
    """Parse a ``POST /iclock/cdata`` body.

    ``form`` is the decoded form mapping. ``raw_body`` is the undecoded body,
    used only to report lines that could not be understood.
    """
    serial = (form.get("SN") or form.get("sn") or "").strip()

    # A handshake may send the serial as a body line instead of a parameter.
    body_lines = [
        line.strip()
        for line in (raw_body or "").splitlines()
        if line.strip()
    ]
    for line in body_lines:
        lowered = line.lower()
        if lowered.startswith("~serialnumber="):
            serial = serial or line.split("=", 1)[1].strip()

    table = (form.get("table") or form.get("Table") or "").strip().upper()

    push = ADMSPush(
        serial_no=serial,
        table=table,
        stamp=form.get("Stamp") or form.get("stamp"),
        comm_key=(
            form.get("CommKey")
            or form.get("commkey")
            or form.get("comm_key")
            or None
        ),
    )

    # Device self-description lines: "~DeviceName=MB20-VL".
    for line in body_lines:
        if line.startswith("~") and "=" in line:
            key, _, value = line[1:].partition("=")
            push.device_info[key.strip()] = value.strip()

    data = form.get("data")
    if data:
        for line in str(data).splitlines():
            if not line.strip():
                continue
            punch = parse_punch_line(line)
            if punch is None:
                push.unparsed.append(line)
            else:
                push.punches.append(punch)

    return push


def is_attendance_push(push: ADMSPush) -> bool:
    """Whether this push carries attendance rows we should store.

    Devices also send ``OPERLOG`` and ``OPTIONS`` tables; those are recorded as
    a heartbeat rather than parsed as punches.
    """
    if push.table == TABLE_ATTLOG:
        return True
    # Some firmware omits `table` but still sends data rows.
    return not push.table and bool(push.punches)


def handshake_response() -> str:
    """The body the device expects from ``GET /iclock/cdata``.

    The device reads these three directives before it will start pushing. They
    are the minimum accepted by ZKTeco firmware; ``Stamp`` and ``OpStamp`` tell
    it which of its records we already hold, and an empty value means "send
    everything", which is the safe default for a new server.
    """
    return (
        "GET OPTION FROM: {serial}\n"
        "Stamp=9999\n"
        "OpStamp=9999\n"
        "ErrorDelay=30\n"
        "Delay=10\n"
        "TransTimes=00:00;14:05\n"
        "TransInterval=1\n"
        "TransFlag=TransData AttLog\n"
        "TimeZone=3\n"
        "Realtime=1\n"
        "Encrypt=0\n"
    )


def format_handshake(serial_no: str) -> str:
    """Fill the handshake template with the requesting device's serial."""
    return handshake_response().format(serial=serial_no or "UNKNOWN")
