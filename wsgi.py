"""WSGI entry point for the EMS application.

Production deploys set ``FLASK_CONFIG=production`` and run this under
gunicorn::

    gunicorn -w 4 -b 0.0.0.0:8000 wsgi:app
"""

from app import create_app
from app.config import config_by_name  # noqa: F401  (re-exported for tooling)

app = create_app()

if __name__ == "__main__":  # pragma: no cover - convenience only
    app.run(debug=True)
