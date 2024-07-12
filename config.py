import os
import secrets
from urllib.parse import urljoin

class Config:
    BASE_DIR = os.path.abspath(os.path.dirname(__file__))
    SECRET_KEY = os.environ.get('SECRET_KEY') or secrets.token_hex(16)
    SQLALCHEMY_DATABASE_URI = 'sqlite:///' + os.path.join(BASE_DIR, 'site.db')
    
    # Celery configurations
    CELERY_BROKER_URL = os.environ.get('CELERY_BROKER_URL', 'amqp://user:password@rabbitmq:5672/')
    CELERY_RESULT_BACKEND = os.environ.get('CELERY_RESULT_BACKEND', 'redis://redis:6379/0')
    
    # Application specific configurations
    RECORDINGS_DIR = os.environ.get('RECORDINGS_DIR', '/recordings/')
    
    # Twitch API configurations
    TWITCH_CLIENT_ID = os.environ.get('TWITCH_CLIENT_ID')
    TWITCH_CLIENT_SECRET = os.environ.get('TWITCH_CLIENT_SECRET')
    TWITCH_WEBHOOK_SECRET = os.environ.get('TWITCH_WEBHOOK_SECRET')
    
    # URL configurations
    BASE_URL = os.environ.get('BASE_URL')
    WEBHOOK_PATH = '/webhook/callback'
    EVENTSUB_WEBHOOK_PORT = int(os.environ.get('EVENTSUB_WEBHOOK_PORT'))
    CALLBACK_URL = urljoin(BASE_URL, WEBHOOK_PATH)

# print(f"TWITCH_CLIENT_ID: {os.getenv('TWITCH_CLIENT_ID')}")
# print(f"TWITCH_CLIENT_SECRET: {os.getenv('TWITCH_CLIENT_SECRET')}")
# print(f"TWITCH_WEBHOOK_SECRET: {os.getenv('TWITCH_WEBHOOK_SECRET')}")
# print(f"BASE_URL: {os.getenv('BASE_URL')}")
# print(f"EVENTSUB_WEBHOOK_PORT: {os.getenv('EVENTSUB_WEBHOOK_PORT')}")
