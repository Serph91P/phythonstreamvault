#!/bin/bash
set -e

echo "Starting entrypoint script"

# Initialize the database
echo "Running database migrations..."
flask db migrate || echo "No new migrations to run"

echo "Upgrading database..."
flask db upgrade

echo "Starting Gunicorn..."
exec gunicorn -c gunicorn.conf.py 'app.wsgi:app' \
    --log-level debug \
    --capture-output \
    --enable-stdio-inheritance \
    --access-logfile - \
    --error-logfile - \
    --log-file -
