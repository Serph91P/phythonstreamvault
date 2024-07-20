import os
import logging
from flask import Flask, render_template, request, session, abort
from flask_migrate import Migrate
from flask_session import Session
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_sqlalchemy import SQLAlchemy
from flask_bcrypt import Bcrypt
from flask_login import LoginManager
from .celery import make_celery, celery
import redis
import secrets
from functools import wraps
import multiprocessing
import asyncio
from app.twitch_api import TwitchAPI, setup_twitch, setup_eventsub

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

db = SQLAlchemy()
bcrypt = Bcrypt()
login_manager = LoginManager()
login_manager.login_view = 'auth.login'
login_manager.login_message = 'Please log in to access this page.'
migrate = Migrate()

def create_app():
    logger.info("Starting create_app function")
    app = Flask(__name__)
    app.config.from_object('config.Config')
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)

    db.init_app(app)
    bcrypt.init_app(app)
    login_manager.init_app(app)
    migrate.init_app(app, db)

    app.config['SESSION_TYPE'] = 'redis'
    app.config['SESSION_REDIS'] = redis.Redis.from_url(app.config['REDIS_URL'])
    Session(app)

    celery.conf.update(app.config)

    @app.context_processor
    def inject_csrf_token():
        return dict(csrf_token=generate_csrf_token())

    with app.app_context():
        init_twitch()

    from app.auth import auth as auth_blueprint
    app.register_blueprint(auth_blueprint, url_prefix='/auth')
    from app.main import main as main_blueprint
    app.register_blueprint(main_blueprint)

    logger.info("Application initialization complete")
    return app

def init_twitch():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(setup_twitch_and_eventsub())

async def setup_twitch_and_eventsub():
    from flask import current_app
    twitch_instance = await setup_twitch(current_app)
    if twitch_instance:
        eventsub_instance = await setup_eventsub(current_app, twitch_instance)
        if eventsub_instance:
            current_app.config['TWITCH_API'] = TwitchAPI(twitch_instance, eventsub_instance)
            current_app.logger.info("Twitch API and EventSub initialized successfully")
        else:
            current_app.logger.error("Failed to initialize EventSub")
    else:
        current_app.logger.error("Failed to initialize Twitch API")

def generate_csrf_token():
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_hex(32)
    return session['csrf_token']

def csrf_protect():
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if request.method == "POST":
                token = session.get('csrf_token')
                if not token or token != request.form.get('csrf_token'):
                    abort(403)
            return f(*args, **kwargs)
        return decorated_function
    return decorator

def run_celery():
    celery.start()

def run_flask(app):
    app.run(host='0.0.0.0', port=8000)

def start_application():
    app = create_app()
    app.app_context().push()

    if os.environ.get('CELERY_WORKER'):
        run_celery()
    else:
        celery_process = multiprocessing.Process(target=run_celery)
        celery_process.start()
        run_flask(app)
        celery_process.join()

if __name__ == "__main__":
    start_application()
