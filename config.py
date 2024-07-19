import os

class Config:
    BASE_DIR = os.path.abspath(os.path.dirname(__file__))
    
    # User-configurable settings
    BASE_URL = os.environ.get('BASE_URL', 'https://streamvault.mebert-server.de')
    TWITCH_CLIENT_ID = os.environ.get('TWITCH_CLIENT_ID')
    TWITCH_CLIENT_SECRET = os.environ.get('TWITCH_CLIENT_SECRET')
    TWITCH_WEBHOOK_SECRET = os.environ.get('TWITCH_WEBHOOK_SECRET')

    # Derived settings
    SERVER_NAME = BASE_URL.replace('https://', '').replace('http://', '')
    CALLBACK_URL = f"{BASE_URL}/webhook/callback"

    # Fixed settings
    CELERY_BROKER_URL = 'amqp://user:password@streamvault_rabbit:5672/'
    CELERY_RESULT_BACKEND = 'redis://streamvault_redis:6379/0'
    SQLALCHEMY_DATABASE_URI = 'postgresql://postgres:postgres@db:5432/postgres'
    
    # Other configurations
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    PREFERRED_URL_SCHEME = 'https'
    FLASK_ENV = 'development'
    FLASK_APP = 'app/__init__.py'
    RECORDINGS_DIR = '/recordings/'
    REDIS_URL = 'redis://streamvault_redis:6379/0'
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'your-secret-key'

    @classmethod
    def get_eventsub_webhook_port(cls):
        return int(os.environ.get('EVENTSUB_WEBHOOK_PORT', 8080))
