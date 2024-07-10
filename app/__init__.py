import asyncio
import threading
import os
from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix
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
    from app.twitch_api import ensure_twitch_initialized
    
    async def run_async_init():
        await ensure_twitch_initialized(app)
    
    def start_background_task():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(run_async_init())
    
    thread = threading.Thread(target=start_background_task)
    thread.start()

def create_app():
    app = Flask(__name__)
    app.config.from_object('config.Config')
    
    app.logger.info("Starting application...")

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)

    db.init_app(app)
    bcrypt.init_app(app)
    login_manager.init_app(app)
    migrate.init_app(app, db)  # Initialize Flask-Migrate here

    if not os.path.exists(os.path.join(app.config['BASE_DIR'], 'migrations')):
        with app.app_context():
            db.create_all()
            # Initialize migrations if not present
            from flask_migrate import init as migrate_init
            migrate_init()
    else:
        app.logger.info("Migrations directory already exists")

    from app.auth import auth as auth_blueprint
    app.register_blueprint(auth_blueprint, url_prefix='/auth')

    from app.main import main as main_blueprint
    app.register_blueprint(main_blueprint)

    from app.twitch_api import ensure_twitch_initialized
    with app.app_context():
        asyncio.run(ensure_twitch_initialized(app))

    app.logger.info("Application initialized successfully")
    return app

app = create_app()
celery = make_celery(app)
