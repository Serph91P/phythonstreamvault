import os
import secrets
from urllib.parse import urljoin

class Config:
    BASE_DIR = os.path.abspath(os.path.dirname(__file__))
    SECRET_KEY = os.environ.get('SECRET_KEY') or secrets.token_hex(16)
    SQLALCHEMY_DATABASE_URI = 'sqlite:///' + os.path.join(BASE_DIR, 'site.db')
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    PREFERRED_URL_SCHEME = 'https'

    #Flask Environment
    FLASK_ENV = 'development'
    FLASK_APP = 'app/__init__.py'
    
    # Celery configurations
    CELERY_BROKER_URL = os.environ.get('CELERY_BROKER_URL', 'amqp://user:password@rabbitmq:5672/')
    CELERY_RESULT_BACKEND = os.environ.get('CELERY_RESULT_BACKEND', 'redis://redis:6379/0')
    CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
    
    # Application specific configurations
    RECORDINGS_DIR = os.environ.get('RECORDINGS_DIR', '/recordings/')
    
    # Twitch API configurations
    TWITCH_CLIENT_ID = os.environ.get('TWITCH_CLIENT_ID')
    TWITCH_CLIENT_SECRET = os.environ.get('TWITCH_CLIENT_SECRET')
    TWITCH_WEBHOOK_SECRET = os.environ.get('TWITCH_WEBHOOK_SECRET')
    
    # URL configurations
    BASE_URL = os.environ.get('BASE_URL')
    WEBHOOK_PATH = '/webhook/callback'
    CALLBACK_URL = urljoin(BASE_URL, WEBHOOK_PATH)
    REDIS_URL = os.environ.get('REDIS_URL', 'redis://streamvault_redis:6379/0')
    WTF_CSRF_TIME_LIMIT = None


    @classmethod
    def get_eventsub_webhook_port(cls):
        return int(os.environ.get('EVENTSUB_WEBHOOK_PORT', 8080))
