import os
import secrets

class Config:
    BASE_DIR = os.path.abspath(os.path.dirname(__file__))
    SECRET_KEY = os.environ.get('SECRET_KEY') or secrets.token_hex(16)
    SQLALCHEMY_DATABASE_URI = 'sqlite:///' + os.path.join(BASE_DIR, 'site.db')
    broker_url = os.environ.get('CELERY_BROKER_URL', 'amqp://user:password@rabbitmq:5672/')
    result_backend = os.environ.get('CELERY_RESULT_BACKEND', 'rpc://')
    RECORDINGS_DIR = os.environ.get('RECORDINGS_DIR', '/recordings/')
    TWITCH_CLIENT_ID = os.environ.get('TWITCH_CLIENT_ID')
    TWITCH_CLIENT_SECRET = os.environ.get('TWITCH_CLIENT_SECRET')
    BASE_URL = os.environ.get('BASE_URL', 'http://localhost:5000')
    WEBHOOK_PATH = '/webhook'
    CALLBACK_URL = f"{BASE_URL}{WEBHOOK_PATH}"
    broker_connection_retry_on_startup = True
    broker_connection_max_retries = None
    broker_heartbeat = 10
    broker_connection_timeout = 30