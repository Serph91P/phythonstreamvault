#!/bin/bash
set -e

# Generate secret keys and passwords if they don't exist
if [ ! -f /usr/src/app/instance/secrets.env ]; then
    echo "Generating secret keys and passwords..."
    SECRET_KEY=$(python -c "import secrets; print(secrets.token_hex(32))")
    WTF_CSRF_SECRET_KEY=$(python -c "import secrets; print(secrets.token_hex(32))")
    DB_PASSWORD=$(python -c "import secrets; print(secrets.token_urlsafe(16))")
    
    # Save the generated values
    echo "SECRET_KEY=$SECRET_KEY" > /usr/src/app/instance/secrets.env
    echo "WTF_CSRF_SECRET_KEY=$WTF_CSRF_SECRET_KEY" >> /usr/src/app/instance/secrets.env
    echo "DB_PASSWORD=$DB_PASSWORD" >> /usr/src/app/instance/secrets.env
fi

# Source the secrets
source /usr/src/app/instance/secrets.env

# Set environment variables
export SECRET_KEY
export WTF_CSRF_SECRET_KEY
export DB_PASSWORD

echo "Starting entrypoint script"

echo "Initializing database..."
python init_db.py

echo "Starting Gunicorn..."
exec gunicorn -c gunicorn.conf.py 'app.wsgi:app' \
    --log-level info \
    --capture-output \
    --enable-stdio-inheritance \
    --access-logfile - \
    --error-logfile - \
    --log-file -
