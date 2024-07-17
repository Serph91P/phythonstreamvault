import os
import logging
from flask import Flask, render_template, request, session
from flask_migrate import Migrate
import flask_migrate
from flask_session import Session
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_sqlalchemy import SQLAlchemy
from flask_bcrypt import Bcrypt
from flask_login import LoginManager
from .celery import make_celery, celery
import redis
from flask_wtf.csrf import CSRFProtect, CSRFError, generate_csrf
from redis.exceptions import LockError

# Logging setup
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)
csrf_logger = logging.getLogger('csrf')
csrf_logger.setLevel(logging.DEBUG)

# Initialize extensions
db = SQLAlchemy()
bcrypt = Bcrypt()
login_manager = LoginManager()
login_manager.login_view = 'auth.login'
login_manager.login_message = 'Please log in to access this page.'
migrate = Migrate()
csrf = CSRFProtect()

def create_app():
    app = Flask(__name__)
    app.config.from_object('config.Config')
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)

    # Initialize extensions with app
    csrf.init_app(app)
    db.init_app(app)
    bcrypt.init_app(app)
    login_manager.init_app(app)
    migrate.init_app(app, db)

    # Session configuration
    app.config['SESSION_TYPE'] = 'redis'
    app.config['SESSION_REDIS'] = redis.Redis.from_url(app.config['REDIS_URL'])
    app.config['WTF_CSRF_TIME_LIMIT'] = None
    app.config['WTF_CSRF_SSL_STRICT'] = False
    app.config['WTF_CSRF_COOKIE_SECURE'] = True
    app.config['WTF_CSRF_COOKIE_HTTPONLY'] = True
    app.config['WTF_CSRF_COOKIE_SAMESITE'] = 'Lax'
    Session(app)

    # CSRF protection
    @app.errorhandler(CSRFError)
    def handle_csrf_error(e):
        csrf_logger.error(f'CSRF Error: {e.description}')
        csrf_logger.debug(f'Request method: {request.method}')
        csrf_logger.debug(f'Request headers: {request.headers}')
        csrf_logger.debug(f'Request form data: {request.form}')
        csrf_logger.debug(f'Session data: {session}')
        return render_template('csrf_error.html', reason=e.description), 400

    @app.before_request
    def log_request_info():
        csrf_logger.debug(f"Request Method: {request.method}")
        csrf_logger.debug(f"Request CSRF token: {request.form.get('csrf_token')}")
        csrf_logger.debug(f"Session CSRF token: {session.get('csrf_token')}")
        csrf_logger.debug(f"Cookie CSRF token: {request.cookies.get('csrf_token')}")
        csrf_logger.debug(f"Session data: {session}")

    @app.after_request
    def log_response_info(response):
        response.set_cookie('csrf_token', generate_csrf())
        csrf_logger.debug(f"Response Status: {response.status}")
        csrf_token = generate_csrf()
        session['csrf_token'] = csrf_token
        response.set_cookie('csrf_token', csrf_token, secure=True, httponly=True, samesite='Lax')
        return response

    @app.errorhandler(CSRFError)
    def handle_csrf_error(e):
        return render_template('csrf_error.html', reason=e.description), 400

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
