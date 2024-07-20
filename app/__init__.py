import os
import logging
from flask import Flask
from flask_migrate import Migrate
from flask_session import Session
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_sqlalchemy import SQLAlchemy
from flask_bcrypt import Bcrypt
from flask_login import LoginManager
from .celery import make_celery, celery
import redis

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

    from app.auth import auth as auth_blueprint
    app.register_blueprint(auth_blueprint, url_prefix='/auth')
    from app.main import main as main_blueprint
    app.register_blueprint(main_blueprint)

    @app.before_first_request
    def initialize_twitch_and_eventsub():
        from app.twitch_api import TwitchAPI, setup_twitch, setup_eventsub
        twitch_instance = setup_twitch(app)
        if twitch_instance:
            eventsub_instance = setup_eventsub(app, twitch_instance)
            if eventsub_instance:
                app.config['TWITCH_API'] = TwitchAPI(twitch_instance, eventsub_instance)
                app.logger.info("Twitch API and EventSub initialized successfully")
            else:
                app.logger.error("Failed to initialize EventSub")
        else:
            app.logger.error("Failed to initialize Twitch API")

    logger.info("Application initialization complete")
    return app
