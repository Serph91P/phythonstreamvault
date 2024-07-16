#!/bin/bash
set -e

echo "Starting entrypoint script"

echo "Initializing database..."
python init_db.py

echo "Starting Gunicorn..."
exec gunicorn -c gunicorn.conf.py 'app.wsgi:app' \
    --log-level debug \
    --capture-output \
    --enable-stdio-inheritance \
    --access-logfile - \
    --error-logfile - \
    --log-file -
