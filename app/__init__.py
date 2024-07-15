import os
import logging
from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_sqlalchemy import SQLAlchemy
from flask_bcrypt import Bcrypt
from flask_login import LoginManager
from flask_migrate import Migrate
from .celery import make_celery, celery

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

db = SQLAlchemy()
bcrypt = Bcrypt()
login_manager = LoginManager()
login_manager.login_view = 'auth.login'
login_manager.login_message = 'Please log in to access this page.'
migrate = Migrate()

def create_app():
    worker_id = os.environ.get('GUNICORN_WORKER_ID', 'Unknown')
    logger.info(f"Worker {worker_id}: Starting create_app function")
    
    app = Flask(__name__)
    app.config.from_object('config.Config')
    
    logger.info(f"Worker {worker_id}: Initializing app components")
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)
    db.init_app(app)
    bcrypt.init_app(app)
    login_manager.init_app(app)
    migrate.init_app(app, db)

    logger.info(f"Worker {worker_id}: Registering blueprints")
    from app.auth import auth as auth_blueprint
    app.register_blueprint(auth_blueprint, url_prefix='/auth')
    from app.main import main as main_blueprint
    app.register_blueprint(main_blueprint)

    if os.environ.get('Container_Type') != 'eventsub':
        logger.info(f"Worker {worker_id}: Initializing Twitch API")
        from app.twitch_api import init_twitch_api
        app.config['TWITCH_API'] = init_twitch_api(app)

    logger.info(f"Worker {worker_id}: Application initialization complete")
    return app

if __name__ == "__main__":
    app = create_app()
    app.logger.info("Starting Flask application")
    app.run(host='0.0.0.0', port=8000)
