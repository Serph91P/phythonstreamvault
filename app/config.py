import os

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'hard_to_guess_string'
    SQLALCHEMY_DATABASE_URI = 'sqlite:///site.db'
    CELERY_BROKER_URL = 'pyamqp://guest@rabbitmq//'
    CELERY_RESULT_BACKEND = 'rpc://'
    RECORDINGS_DIR = '/recordings/'
