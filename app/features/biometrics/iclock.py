"""ZKTeco ADMS ("iClock") endpoints.

These are the routes a physical terminal talks to. They differ from the JSON
webhook in :mod:`app.features.biometrics.blueprint` because the hardware cannot
meet that endpoint's requirements — see :mod:`app.features.biometrics.adms` for
the wire-format reasoning.

Point the device at::

    Server Address: kenems.up.railway.app
    Server Port:    443
    Comm Key:       <the secret shown when you registered the device>

The device picks the paths itself; they are the firmware's, not a choice:

    GET  /iclock/cdata?SN=<serial>&options=all   -> handshake, device starts pushing
    POST /iclock/cdata?SN=<serial>&table=ATTLOG  -> attendance records
    GET  /iclock/getrequest?SN=<serial>          -> polling for server commands

Every reply is plain text. ``OK`` means accepted; anything else makes the device
hold the record and retry, which is what should happen when a write fails.
"""

from __future__ import annotations

from flask import Blueprint, Response, request

from app.core.logging import get_logger
from app.features.biometrics.adms import (
    REPLY_OK,
    format_handshake,
    is_attendance_push,
    parse_cdata,
)
from app.features.biometrics.exceptions import DeviceAuthError
from app.features.biometrics.controller import BiometricsController

logger = get_logger(__name__)

iclock_bp = Blueprint("iclock", __name__, url_prefix="/iclock")

_controller = BiometricsController()

#: Replies are plain text and must not be cached or rewritten by a proxy.
_TEXT_HEADERS = {"Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store"}


def _plain(body: str, status: int = 200) -> Response:
    return Response(body, status=status, headers=_TEXT_HEADERS)


def _serial_from_request() -> str:
    """The device's serial number.

    Devices send it as a query parameter on every call; some firmware also puts
    it in the body, which :func:`parse_cdata` picks up.
    """
    return (
        request.args.get("SN")
        or request.args.get("sn")
        or request.form.get("SN")
        or request.form.get("sn")
        or ""
    ).strip()


def _authenticate(serial_no: str, comm_key: str | None):
    """Resolve and authorise the calling device, or raise :class:`DeviceAuthError`.

    A device cannot send custom headers, so the credential arrives as the
    ``CommKey`` field — the only secret the hardware can hold. An
    ``X-Device-Secret`` header is still honoured, because a bridge process or a
    test harness can send one.

    A terminal whose communication password was never set sends the factory
    value, so that is accepted as well: refusing it would mean no attendance
    data from a device that is otherwise working correctly.
    """
    header_secret = request.headers.get("X-Device-Secret")
    secret = header_secret or comm_key
    if not serial_no:
        raise DeviceAuthError("The device did not identify itself.")
    return _controller.authenticate_adms_device(serial_no, secret)


def _authenticate_or_refuse(serial_no: str, comm_key: str | None):
    """Authenticate, returning ``(device, None)`` or ``(None, response)``.

    The app-wide ``AppError`` handler answers JSON, which a terminal cannot
    read. Auth failures are therefore converted here so the device receives the
    plain-text ``ERROR`` it understands — and, because it is not ``OK``, the
    device holds its records and retries rather than discarding them.
    """
    try:
        return _authenticate(serial_no, comm_key), None
    except DeviceAuthError:
        logger.warning(
            "ADMS request refused SN=%s path=%s ip=%s",
            serial_no or "-",
            request.path,
            request.remote_addr,
        )
        return None, _plain("ERROR", 401)


# ---------------------------------------------------------------------------
# Handshake
# ---------------------------------------------------------------------------
@iclock_bp.get("/cdata")
def handshake():
    """Answer the device's opening request so it starts pushing.

    The device calls this before its first push, and again after any change. A
    reply it cannot parse leaves it stuck in this loop, so the body is generated
    by a pure formatter and the credentials are checked first.
    """
    serial_no = _serial_from_request()
    form = request.args.to_dict()
    comm_key = form.get("CommKey") or form.get("commkey")

    device, refusal = _authenticate_or_refuse(serial_no, comm_key)
    if refusal is not None:
        return refusal

    # A successful handshake proves the device is configured correctly *and*
    # authorised. That is worth recording as "working" immediately: waiting for
    # the first punch would leave a healthy device looking offline until someone
    # happened to scan, which is exactly the confusion the two timestamps exist
    # to prevent.
    _controller.heartbeat_for_serial(device.serial_no)

    logger.info(
        "ADMS handshake SN=%s options=%s commkey=%s",
        device.serial_no,
        request.args.get("options"),
        "supplied" if comm_key else "absent",
    )
    return _plain(format_handshake(device.serial_no))


# ---------------------------------------------------------------------------
# Data push
# ---------------------------------------------------------------------------
@iclock_bp.post("/cdata")
def receive_cdata():
    """Accept pushed records.

    Responds ``OK`` only when every record in the batch was stored. A partial
    failure answers non-``OK`` so the device retries the batch; ingestion is
    idempotent per punch, so the retry cannot create duplicates.
    """
    serial_no = _serial_from_request()
    raw_body = request.get_data(as_text=True) or ""
    push = parse_cdata(request.form.to_dict(), raw_body=raw_body)
    serial_no = serial_no or push.serial_no

    device, refusal = _authenticate_or_refuse(serial_no, push.comm_key)
    if refusal is not None:
        return refusal

    # A description/options push carries no punches: record the heartbeat only.
    if not is_attendance_push(push):
        _controller.heartbeat(device=device)
        logger.info(
            "ADMS %s push from SN=%s (no punches, %d info line(s))",
            push.table or "unlabelled",
            device.serial_no,
            len(push.device_info),
        )
        return _plain(REPLY_OK)

    logger.info(
        "ADMS ATTLOG from SN=%s: %d punch(es), %d unparsed",
        device.serial_no,
        len(push.punches),
        len(push.unparsed),
    )

    stored = 0
    failed = 0
    for punch in push.punches:
        try:
            _controller.ingest_adms_punch(device=device, punch=punch)
            stored += 1
        except Exception:  # noqa: BLE001 - one bad row must not lose the batch
            failed += 1
            logger.exception(
                "ADMS punch failed SN=%s user=%s time=%s",
                device.serial_no,
                punch.device_user_id,
                punch.punch_time,
            )

    if push.unparsed:
        logger.warning(
            "ADMS SN=%s sent %d unparseable line(s); first=%r",
            device.serial_no,
            len(push.unparsed),
            push.unparsed[0][:120],
        )

    if failed:
        # Non-OK reply so the device retains and retries the batch.
        return _plain(f"ERROR {failed}", 500)

    if not push.punches and push.unparsed:
        # Nothing usable at all: ask for a retry rather than silently dropping.
        return _plain("ERROR", 400)

    logger.info("ADMS SN=%s stored %d punch(es)", device.serial_no, stored)
    return _plain(REPLY_OK)


# ---------------------------------------------------------------------------
# Command polling
# ---------------------------------------------------------------------------
@iclock_bp.get("/getrequest")
def get_request():
    """The device polling for server-initiated commands.

    We issue none, so the reply is an empty command list. Answering keeps the
    device in its normal loop; refusing would make it back off and delay pushes.
    """
    serial_no = _serial_from_request()
    device, refusal = _authenticate_or_refuse(
        serial_no, request.args.get("CommKey")
    )
    if refusal is not None:
        return refusal

    _controller.heartbeat_for_serial(device.serial_no)
    return _plain(REPLY_OK)


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------
@iclock_bp.get("/ping")
def ping():
    """Unauthenticated liveness probe.

    Lets you confirm from a browser that the endpoint is reachable over the
    public URL before touching the device's configuration. It exposes nothing —
    no serial numbers, no data — so it is safe to leave open.
    """
    return _plain("OK")
