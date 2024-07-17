import os
import logging
from flask import Flask, render_template, request, session, abort
from flask_migrate import Migrate
import flask_migrate
from flask_session import Session
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_sqlalchemy import SQLAlchemy
from flask_bcrypt import Bcrypt
from flask_login import LoginManager
from .celery import make_celery, celery
import redis
import secrets
from functools import wraps

# Logging setup
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Initialize extensions
db = SQLAlchemy()
bcrypt = Bcrypt()
login_manager = LoginManager()
login_manager.login_view = 'auth.login'
login_manager.login_message = 'Please log in to access this page.'
migrate = Migrate()

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

def create_app():
    app = Flask(__name__)
    app.config.from_object('config.Config')
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)

    # Initialize extensions with app
    db.init_app(app)
    bcrypt.init_app(app)
    login_manager.init_app(app)
    migrate.init_app(app, db)

    # Session configuration
    app.config['SESSION_TYPE'] = 'redis'
    app.config['SESSION_REDIS'] = redis.Redis.from_url(app.config['REDIS_URL'])
    Session(app)

    @app.context_processor
    def inject_csrf_token():
        return dict(csrf_token=generate_csrf_token())

    # Register blueprints
    from app.auth import auth as auth_blueprint
    app.register_blueprint(auth_blueprint, url_prefix='/auth')
    from app.main import main as main_blueprint
    app.register_blueprint(main_blueprint)

    logger.info("Application initialization complete")
    return app

if __name__ == "__main__":
    app = create_app()
    app.logger.info("Starting Flask application")
    app.run(host='0.0.0.0', port=8000)
