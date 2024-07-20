#!/bin/bash
set -e

echo "Starting entrypoint script"

mkdir -p /usr/src/app/instance
chmod 700 /usr/src/app/instance

if [ -d "/usr/src/app/instance" ]; then
    echo "Instance directory exists and is accessible"
else
    echo "Failed to create or access instance directory"
    exit 1
fi

# Generate secret keys if they don't exist
if [ ! -f /usr/src/app/instance/secrets.env ]; then
    echo "Generating secret keys..."
    SECRET_KEY=$(python -c "import secrets; print(secrets.token_hex(32))")
    WTF_CSRF_SECRET_KEY=$(python -c "import secrets; print(secrets.token_hex(32))")
    
    # Save the generated values
    echo "SECRET_KEY=$SECRET_KEY" > /usr/src/app/instance/secrets.env
    echo "WTF_CSRF_SECRET_KEY=$WTF_CSRF_SECRET_KEY" >> /usr/src/app/instance/secrets.env
fi

# Source the secrets
source /usr/src/app/instance/secrets.env
echo "Secrets generated and sourced"

# Set environment variables
export SECRET_KEY
export WTF_CSRF_SECRET_KEY

echo "Waiting for database to be ready..."
until PGPASSWORD=postgres psql -h streamvault_db -U postgres -c '\q'; do
  >&2 echo "Postgres is unavailable - sleeping"
  sleep 1
done

echo "Database is ready!"

echo "Initializing database..."
python init_db.py
if [ $? -eq 0 ]; then
    echo "Database initialization completed successfully"
else
    echo "Database initialization failed"
    exit 1
fi

echo "Environment variables:"
env
echo "Current directory contents:"
ls -la

echo "Starting Gunicorn..."
exec gunicorn -c gunicorn.conf.py 'app:create_app()' --bind 0.0.0.0:8000 --log-level debug
