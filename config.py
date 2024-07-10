import os
import secrets

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
    CALLBACK_URL = f"{BASE_URL}{WEBHOOK_PATH}"