import threading
import asyncio
import traceback
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

def run_async(app, coro):
    async def wrapper():
        with app.app_context():
            await coro

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(wrapper())
    finally:
        loop.close()

def create_app():
    app = Flask(__name__)
    app.config.from_object('config.Config')
    db.init_app(app)
    bcrypt.init_app(app)
    login_manager.init_app(app)
    migrate.init_app(app, db)

    from app.auth import auth as auth_blueprint
    app.register_blueprint(auth_blueprint, url_prefix='/auth')

    from app.main import main as main_blueprint
    app.register_blueprint(main_blueprint)

    from app.twitch_api import setup_twitch, setup_eventsub, ensure_twitch_initialized

    @app.before_request
    def setup_twitch_wrapper():
        if not hasattr(app, 'twitch_setup_done'):
            def run_setup():
                try:
                    run_async(app, ensure_twitch_initialized(app))
                    app.logger.info("Twitch and EventSub initialization completed")
                except Exception as e:
                    app.logger.error(f"Error in Twitch setup: {str(e)}")
                    app.logger.error(traceback.format_exc())

            thread = threading.Thread(target=run_setup)
            thread.start()
            app.twitch_setup_done = True

    return app

app = create_app()
celery = make_celery(app)