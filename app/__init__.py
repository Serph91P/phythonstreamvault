import asyncio
import traceback
import threading
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_bcrypt import Bcrypt
from flask_login import LoginManager
from flask_migrate import Migrate
from .celery import make_celery, celery

db = SQLAlchemy()
bcrypt = Bcrypt()
login_manager = LoginManager()
login_manager.login_view = 'auth.login'
login_manager.login_message = 'Please log in to access this page.'
migrate = Migrate()

def async_init(app):
    async def init_twitch():
        from app.twitch_api import ensure_twitch_initialized
        await ensure_twitch_initialized(app)

    def run_async_init():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(init_twitch())

    thread = threading.Thread(target=run_async_init)
    thread.start()

def create_app():
    app = Flask(__name__)
    app.config.from_object('config.Config')
    
    # Set up logging
    import logging
    logging.basicConfig(level=logging.INFO)
    app.logger.info("Starting application...")

    try:
        db.init_app(app)
        bcrypt.init_app(app)
        login_manager.init_app(app)
        migrate.init_app(app, db)

        from app.auth import auth as auth_blueprint
        app.register_blueprint(auth_blueprint, url_prefix='/auth')

        from app.main import main as main_blueprint
        app.register_blueprint(main_blueprint)

        # Start Twitch API initialization in the background
        async_init(app)

        app.logger.info("Application initialized successfully")
    except Exception as e:
        app.logger.error(f"Error initializing application: {str(e)}")
        app.logger.error(traceback.format_exc())

    return app

app = create_app()
celery = make_celery(app)