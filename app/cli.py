"""Flask CLI commands.

Kept deliberately thin: each command resolves a service or a job ``*_impl``
function, runs it, and prints a short summary.  This is also the supported way
to run jobs without Redis.
"""

from __future__ import annotations

import importlib
import json
from datetime import date, datetime, timedelta

import click
from flask import Flask, current_app

from app.core.database import db
from app.core.logging import get_logger
from app.jobs import JOB_REGISTRY

logger = get_logger(__name__)


def register_cli(app: Flask) -> None:
    """Attach every command to ``app``."""

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------
    @app.cli.command("seed")
    @click.option("--no-demo", is_flag=True, help="Seed rules only, no sample people.")
    def seed_command(no_demo: bool) -> None:
        """Seed initial data (idempotent)."""
        from app.seed import seed_all

        created = seed_all(with_demo_data=not no_demo)
        click.echo(json.dumps(created, indent=2, default=str))
        click.secho("Seed complete.", fg="green")

    @app.cli.command("seed-defaults")
    def seed_defaults_command() -> None:
        """Create the global shift policy and default rule ladders.

        Safe to run in production and safe to run repeatedly: it creates only
        what is missing and no accounts at all. A fresh production database needs
        this, or attendance is never classified and every leave request is
        rejected for a missing rule.
        """
        from app.seed import seed_defaults

        created = seed_defaults()
        click.echo(json.dumps(created, indent=2, default=str))
        click.secho("Defaults seeded.", fg="green")

    @app.cli.command("seed-minimal")
    def seed_minimal_command() -> None:
        """Seed the admin user and global rules (development only)."""
        from app.seed import seed_minimal

        created = seed_minimal()
        click.echo(json.dumps(created, indent=2, default=str))
        click.secho("Minimal seed complete.", fg="green")

    @app.cli.command("create-admin")
    @click.option("--email", required=True, help="The administrator's email address.")
    @click.option(
        "--password",
        prompt=True,
        hide_input=True,
        confirmation_prompt=True,
        help="Password (prompted securely if omitted).",
    )
    @click.option("--role", default="ADMIN", show_default=True, help="ADMIN or HR.")
    def create_admin_command(email: str, password: str, role: str) -> None:
        """Create (or re-password) an administrator with real credentials.

        Use this in production instead of ``flask seed``: the seeder creates a
        well-known account with a published default password, which is fine for
        local development and dangerous on the public internet.
        """
        from app.core.database import db
        from app.features.auth.constants import Role
        from app.features.auth.service import AuthService

        wanted = role.strip().upper()
        if wanted not in {Role.ADMIN.value, Role.HR.value}:
            raise click.ClickException(f"Unknown role {role!r}; use ADMIN or HR.")

        service = AuthService()
        existing = service.get_by_email(email)
        if existing is not None:
            service.set_password(existing, password)
            if existing.role != wanted:
                existing.role = wanted
            db.session.commit()
            click.secho(
                f"Updated {email} ({wanted}). "
                f"They must change this password at next sign-in.",
                fg="green",
            )
            return

        service.create_user(
            email=email,
            password=password,
            role=wanted,
            # The password was typed on a shared shell, so require the operator
            # to choose their own at first sign-in.
            must_change_password=True,
        )
        db.session.commit()
        click.secho(
            f"Created {wanted} login for {email}. "
            f"They must change this password at first sign-in.",
            fg="green",
        )

    @app.cli.command("reset-demo")
    @click.confirmation_option(prompt="Delete all transactional demo data?")
    def reset_demo_command() -> None:
        """Delete transactional rows (attendance, leave, notifications)."""
        from app.seed import reset_demo_data

        deleted = reset_demo_data()
        click.echo(json.dumps(deleted, indent=2))
        click.secho("Demo data cleared.", fg="green")

    @app.cli.command("create-tables")
    def create_tables_command() -> None:
        """Create tables directly (development convenience, not migrations)."""
        db.create_all()
        click.secho(f"Created {len(db.metadata.tables)} tables.", fg="green")

    @app.cli.command("drop-tables")
    @click.confirmation_option(prompt="Drop every table in this database?")
    def drop_tables_command() -> None:
        """Drop every table (destructive)."""
        db.drop_all()
        click.secho("All tables dropped.", fg="yellow")

    # ------------------------------------------------------------------
    # Leave balances
    # ------------------------------------------------------------------
    @app.cli.command("allocate-balances")
    @click.option("--year", type=int, default=None, help="Year to allocate (default: current).")
    @click.option("--overwrite", is_flag=True, help="Reset entitlements from the rules.")
    def allocate_balances_command(year: int | None, overwrite: bool) -> None:
        """Backfill leave entitlements for a year."""
        from app.jobs.allocate_balances import allocate_balances_impl

        summary = allocate_balances_impl(year=year, overwrite=overwrite)
        click.echo(json.dumps(summary, indent=2))
        click.secho("Balances allocated.", fg="green")

    @app.cli.command("balance-adjust")
    @click.option("--employee-no", required=True)
    @click.option("--leave-type", required=True)
    @click.option("--year", type=int, default=None)
    @click.option("--entitled", type=int, default=None)
    @click.option("--used", type=int, default=None)
    @click.option("--pending", type=int, default=None)
    def balance_adjust_command(
        employee_no: str,
        leave_type: str,
        year: int | None,
        entitled: int | None,
        used: int | None,
        pending: int | None,
    ) -> None:
        """Adjust one employee's leave balance."""
        from app.features.employees.repository import EmployeeRepository
        from app.features.leave.service import LeaveService

        employee = EmployeeRepository().get_by_employee_no(employee_no)
        if employee is None:
            raise click.ClickException(f"No employee with number {employee_no!r}.")

        try:
            service = LeaveService()
            balance = service.adjust_balance(
                employee_id=employee.id,
                year=year or date.today().year,
                leave_type=leave_type,
                entitled=entitled,
                used=used,
                pending=pending,
            )
            db.session.commit()
        except Exception as exc:  # noqa: BLE001 - surface as a CLI error
            db.session.rollback()
            raise click.ClickException(str(exc)) from exc

        click.secho(
            f"{employee.employee_no} {balance.leave_type} {balance.year}: "
            f"entitled={balance.entitled} used={balance.used} "
            f"pending={balance.pending} available={balance.available}",
            fg="green",
        )

    # ------------------------------------------------------------------
    # Biometric devices
    # ------------------------------------------------------------------
    @app.cli.group("device")
    def device_group() -> None:
        """Manage biometric devices."""

    @device_group.command("add")
    @click.option("--sn", "serial_no", required=True, help="Device serial number.")
    @click.option("--location", default=None)
    @click.option("--vendor", default="zkteco")
    @click.option("--secret", default=None, help="Use a fixed secret instead of generating one.")
    def device_add(serial_no: str, location: str | None, vendor: str, secret: str | None) -> None:
        """Register a biometric device."""
        from app.features.biometrics.service import BiometricsService

        try:
            device, plaintext = BiometricsService().register_device(
                serial_no=serial_no, location=location, vendor=vendor, secret=secret
            )
            db.session.commit()
        except Exception as exc:  # noqa: BLE001
            db.session.rollback()
            raise click.ClickException(str(exc)) from exc

        click.secho(f"Registered {device.serial_no} ({device.vendor}).", fg="green")
        click.secho(f"Shared secret: {plaintext}", fg="yellow", bold=True)
        click.echo("Store this now — it is not recoverable.")

    @device_group.command("rotate")
    @click.option("--sn", "serial_no", required=True)
    @click.option("--secret", default=None)
    def device_rotate(serial_no: str, secret: str | None) -> None:
        """Rotate a device's shared secret."""
        from app.features.biometrics.service import BiometricsService

        service = BiometricsService()
        device = service.devices.get_by_serial(serial_no)
        if device is None:
            raise click.ClickException(f"No device with serial {serial_no!r}.")
        try:
            _device, plaintext = service.rotate_secret(device.id, secret=secret)
            db.session.commit()
        except Exception as exc:  # noqa: BLE001
            db.session.rollback()
            raise click.ClickException(str(exc)) from exc

        click.secho("Secret rotated.", fg="green")
        click.secho(f"New shared secret: {plaintext}", fg="yellow", bold=True)

    @device_group.command("map")
    @click.option("--sn", "serial_no", required=True)
    @click.option("--employee-no", required=True)
    @click.option("--device-user-id", required=True)
    def device_map(serial_no: str, employee_no: str, device_user_id: str) -> None:
        """Map a device-local user id onto an employee."""
        from app.features.biometrics.service import BiometricsService
        from app.features.employees.repository import EmployeeRepository

        employee = EmployeeRepository().get_by_employee_no(employee_no)
        if employee is None:
            raise click.ClickException(f"No employee with number {employee_no!r}.")

        service = BiometricsService()
        device = service.devices.get_by_serial(serial_no)
        if device is None:
            raise click.ClickException(f"No device with serial {serial_no!r}.")

        try:
            service.map_employee(
                device_id=device.id,
                employee_id=employee.id,
                device_user_id=device_user_id,
            )
            db.session.commit()
        except Exception as exc:  # noqa: BLE001
            db.session.rollback()
            raise click.ClickException(str(exc)) from exc

        click.secho(
            f"Mapped device user {device_user_id} on {serial_no} to {employee.employee_no}.",
            fg="green",
        )

    @device_group.command("list")
    def device_list() -> None:
        """List registered devices."""
        from app.features.biometrics.service import BiometricsService

        devices = BiometricsService().list_devices()
        if not devices:
            click.echo("No devices registered.")
            return
        for device in devices:
            state = "online" if device.is_online else "offline"
            click.echo(
                f"{device.serial_no:<16} {device.vendor:<12} {state:<8} "
                f"{(device.location or '-'):<32} last_seen={device.last_seen_at}"
            )

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------
    @app.cli.command("run-job")
    @click.argument("name")
    @click.option(
        "--arg",
        "args",
        multiple=True,
        help="Job argument as key=value (repeatable).",
    )
    def run_job(name: str, args: tuple[str, ...]) -> None:
        """Run any registered background job synchronously (needs no Redis)."""
        if name not in JOB_REGISTRY:
            available = ", ".join(sorted(JOB_REGISTRY))
            raise click.ClickException(
                f"Unknown job {name!r}. Available jobs: {available}."
            )

        kwargs = _parse_job_args(args)
        module_path, _, attribute = JOB_REGISTRY[name].partition(":")
        module = importlib.import_module(module_path)
        function = getattr(module, attribute)

        click.echo(f"Running {name} with {kwargs or 'no arguments'} ...")
        try:
            result = function(**kwargs)
        except Exception as exc:  # noqa: BLE001 - report and fail cleanly
            db.session.rollback()
            raise click.ClickException(f"{name} failed: {exc}") from exc

        click.echo(json.dumps(result, indent=2, default=str))
        click.secho(f"{name} complete.", fg="green")

    @app.cli.command("jobs")
    def list_jobs() -> None:
        """List the registered background jobs."""
        from celery_worker import celery

        click.secho("Registered jobs (flask run-job <name>):", bold=True)
        for name in sorted(JOB_REGISTRY):
            click.echo(f"  {name}")
        click.echo("")
        click.secho("Celery beat schedule:", bold=True)
        for entry, config in (celery.conf.beat_schedule or {}).items():
            click.echo(f"  {entry:<24} {config['task']}  every {config['schedule']}")

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------
    @app.cli.command("check-schema")
    def check_schema_command() -> None:
        """Compare the database against the models; exit non-zero on a gap.

        Run this first when a deployed feature misbehaves in a way that looks
        impossible — a deploy that landed before its migration produces failures
        far from the cause.
        """
        from app.core.schema_check import check_schema

        report = check_schema(app)
        click.echo(
            f"alembic revision : {report.revision or '(none — migrations never ran)'}"
        )

        if report.missing_tables:
            click.secho("MISSING TABLES", fg="red", bold=True)
            for name in report.missing_tables:
                click.echo(f"  {name}")

        if report.missing_columns:
            click.secho("MISSING COLUMNS — the code expects these", fg="red", bold=True)
            for name in report.missing_columns:
                click.echo(f"  {name}")

        if report.extra_columns:
            click.secho("EXTRA COLUMNS — present in the database only", fg="yellow")
            for name in report.extra_columns:
                click.echo(f"  {name}")

        if report.is_stale:
            click.echo("")
            click.secho(
                "The database is behind the code. Run:\n"
                "  flask --app wsgi db upgrade",
                fg="red",
                bold=True,
            )
            raise SystemExit(1)

        click.secho("Schema matches the models (0 gap(s)).", fg="green")

    @app.cli.command("reprocess-punches")
    @click.option("--limit", default=500, show_default=True,
                  help="Maximum punches to replay.")
    def reprocess_punches_command(limit: int) -> None:
        """Replay punches that were stored but never turned into attendance.

        A punch is committed as soon as it arrives, before attendance is
        resolved, so a failure downstream leaves the punch safely stored and
        marked unprocessed rather than losing it. This replays those.

        Safe to run repeatedly: attendance resolution is idempotent per punch.
        """
        from app.core.database import db
        from app.features.attendance.service import AttendanceService
        from app.features.biometrics.models import BiometricRawLog
        from app.features.biometrics.service import BiometricsService

        biometrics = BiometricsService()
        attendance = AttendanceService()
        rows = biometrics.raw_logs.unprocessed(limit=limit)

        if not rows:
            click.secho("No unprocessed punches.", fg="green")
            return

        click.echo(f"Replaying {len(rows)} unprocessed punch(es)...")
        resolved = 0
        unmapped = 0
        failed = 0

        for raw in rows:
            mapping = biometrics.maps.resolve(
                device_id=raw.device_id, device_user_id=raw.device_user_id
            )
            if mapping is None:
                # Still unmapped: leave it unprocessed so it appears in the
                # device's unmatched list and can be resolved by mapping.
                unmapped += 1
                continue
            try:
                employee = mapping.employee
                attendance.apply_punch(
                    employee_id=employee.id,
                    department_id=employee.department_id,
                    tz_name=employee.tz,
                    punch_time=raw.punch_time,
                    punch_type=raw.punch_type,
                )
                biometrics.mark_processed(raw)
                db.session.commit()
                resolved += 1
            except Exception as error:  # noqa: BLE001 - keep going through the batch
                db.session.rollback()
                failed += 1
                logger.warning(
                    "reprocess failed for punch id=%s: %s: %s",
                    raw.id,
                    type(error).__name__,
                    error,
                )

        click.echo(
            f"  resolved={resolved}  unmapped={unmapped}  failed={failed}"
        )
        if failed:
            click.secho(
                "Some punches still failed — check the schema "
                "(flask --app wsgi check-schema) and the logs.",
                fg="yellow",
            )
            raise SystemExit(1)
        click.secho("Done.", fg="green")

    @app.cli.command("purge-test-punches")
    @click.option("--serial", default=None, help="Only this device serial.")
    @click.option("--user", "device_user_id", default="1", show_default=True,
                  help="Device user id the simulator posts as.")
    @click.option("--timestamp", default="2026-10-16 08:12:33", show_default=True,
                  help="Punch time the simulator sends (UTC).")
    @click.option("--yes", is_flag=True,
                  help="Delete. Without this it only reports what it found.")
    def purge_test_punches_command(
        serial: str | None,
        device_user_id: str,
        timestamp: str,
        yes: bool,
    ) -> None:
        """Remove punches written by ``tools/simulate_device.py``.

        The simulator posts a fixed line — device user ``1`` at
        ``2026-10-16 08:12:33`` — so a test run leaves a punch in whatever
        database it was pointed at. Against production that is a fake punch
        attributed to whichever employee maps to device user 1, and it pollutes
        the live stream.

        The match is deliberately narrow (that exact user *and* that exact
        moment, to the minute) so a real punch cannot be caught by it. The
        attendance row the punch produced is removed only when its clock-in is
        that same moment, i.e. when the simulator is what created it.

        Runs as a dry run unless ``--yes`` is passed.
        """
        from app.core.database import db
        from app.features.attendance.models import AttendanceDaily
        from app.features.biometrics.models import BiometricDevice, BiometricRawLog

        try:
            _date, moment = timestamp.split(" ", 1)
            target = datetime.fromisoformat(f"{_date}T{moment}")
        except ValueError:
            raise click.ClickException(
                f"--timestamp must look like '2026-10-16 08:12:33', got {timestamp!r}"
            )

        window_start = target - timedelta(minutes=1)
        window_end = target + timedelta(minutes=1)

        query = BiometricRawLog.query.filter(
            BiometricRawLog.device_user_id == str(device_user_id),
            BiometricRawLog.punch_time >= window_start,
            BiometricRawLog.punch_time <= window_end,
        )
        if serial:
            device = BiometricDevice.query.filter_by(serial_no=serial.strip()).first()
            if device is None:
                raise click.ClickException(f"Device {serial!r} is not registered.")
            query = query.filter(BiometricRawLog.device_id == device.id)

        logs = query.all()
        if not logs:
            click.secho(
                f"No simulator punches found (user {device_user_id} at {timestamp}).",
                fg="green",
            )
            return

        click.secho(f"Found {len(logs)} matching punch(es):", bold=True)
        for raw in logs:
            device = db.session.get(BiometricDevice, raw.device_id)
            click.echo(
                f"  id={raw.id} device={device.serial_no if device else '?'} "
                f"user={raw.device_user_id} at={raw.punch_time} "
                f"processed={raw.processed}"
            )

        # The attendance row this punch produced, identified by its clock-in.
        affected_days = []
        for raw in logs:
            mapping = (
                db.session.query(BiometricRawLog)
                .filter(BiometricRawLog.id == raw.id)
                .first()
            )
            from app.features.biometrics.models import BiometricEmployeeMap

            match = BiometricEmployeeMap.query.filter_by(
                device_id=raw.device_id, device_user_id=raw.device_user_id
            ).first()
            if match is None:
                continue
            row = AttendanceDaily.query.filter(
                AttendanceDaily.employee_id == match.employee_id,
                AttendanceDaily.work_date == raw.punch_time.date(),
            ).first()
            if row is None:
                continue
            # Only when the simulator's punch is what opened the day.
            if row.clock_in is not None and (
                abs((row.clock_in.replace(tzinfo=None) - target).total_seconds()) <= 60
            ):
                affected_days.append(row)

        if affected_days:
            click.secho(
                f"\nAlso removing {len(affected_days)} attendance row(s) it created:",
                bold=True,
            )
            for row in affected_days:
                click.echo(
                    f"  employee_id={row.employee_id} date={row.work_date} "
                    f"status={row.status} clock_in={row.clock_in}"
                )

        if not yes:
            click.echo("")
            click.secho(
                "Dry run — nothing deleted. Re-run with --yes to remove these.",
                fg="yellow",
                bold=True,
            )
            return

        for row in affected_days:
            db.session.delete(row)
        for raw in logs:
            db.session.delete(raw)
        db.session.commit()

        click.secho(
            f"Deleted {len(logs)} punch(es) and {len(affected_days)} attendance row(s).",
            fg="green",
            bold=True,
        )

    @app.cli.command("routes")
    def routes_command() -> None:
        """Print every registered URL rule."""
        rules = sorted(app.url_map.iter_rules(), key=lambda rule: str(rule))
        for rule in rules:
            methods = ",".join(sorted(rule.methods - {"HEAD", "OPTIONS"}))
            click.echo(f"{methods:<24} {rule}  ->  {rule.endpoint}")

    @app.cli.command("check")
    def check_command() -> None:
        """Sanity-check configuration and database connectivity."""
        from sqlalchemy import text

        click.secho(f"Config: {current_app.config.get('ENV', 'unknown')}", bold=True)
        click.echo(f"  debug           : {app.debug}")
        click.echo(f"  database        : {_redact(app.config['SQLALCHEMY_DATABASE_URI'])}")
        click.echo(f"  csrf protected  : {app.config.get('JWT_COOKIE_CSRF_PROTECT')}")
        click.echo(f"  cors origins    : {app.config.get('CORS_ORIGINS')}")
        click.echo(f"  timezone        : {app.config.get('DEFAULT_TIMEZONE')}")

        try:
            db.session.execute(text("SELECT 1"))
            tables = len(db.metadata.tables)
            click.secho(f"Database reachable. {tables} tables registered.", fg="green")
        except Exception as exc:  # noqa: BLE001
            db.session.rollback()
            click.secho(f"Database unreachable: {exc}", fg="red")
            raise click.ClickException("Database check failed.") from exc

        from app.jobs import JOB_REGISTRY as registry

        click.echo(f"  jobs registered : {len(registry)}")


def _parse_job_args(args: tuple[str, ...]) -> dict:
    """Turn ``key=value`` pairs into typed kwargs."""
    kwargs: dict = {}
    for item in args:
        if "=" not in item:
            raise click.ClickException(
                f"Malformed --arg {item!r}; expected key=value."
            )
        key, _, raw = item.partition("=")
        key = key.strip().replace("-", "_")
        raw = raw.strip()

        lowered = raw.lower()
        if lowered in {"true", "false"}:
            kwargs[key] = lowered == "true"
            continue
        if lowered in {"none", "null", ""}:
            kwargs[key] = None
            continue
        try:
            kwargs[key] = int(raw)
            continue
        except ValueError:
            pass
        try:
            kwargs[key] = date.fromisoformat(raw)
            continue
        except ValueError:
            pass
        try:
            kwargs[key] = datetime.fromisoformat(raw)
            continue
        except ValueError:
            pass
        kwargs[key] = raw
    return kwargs


def _redact(uri: str) -> str:
    """Hide the password portion of a database URI."""
    if "@" not in uri or "://" not in uri:
        return uri
    scheme, _, rest = uri.partition("://")
    credentials, _, host = rest.partition("@")
    if ":" in credentials:
        user, _, _password = credentials.partition(":")
        credentials = f"{user}:***"
    return f"{scheme}://{credentials}@{host}"
