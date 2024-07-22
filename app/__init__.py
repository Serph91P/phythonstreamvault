import os
import logging
from flask import Flask, abort, request, session
from flask_sqlalchemy import SQLAlchemy
from flask_bcrypt import Bcrypt
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_session import Session
from werkzeug.middleware.proxy_fix import ProxyFix
from config import Config
import redis
import secrets
from functools import wraps
from uuid import uuid4
from datetime import timedelta
import pickle
from flask.sessions import SessionInterface, SessionMixin
from werkzeug.datastructures import CallbackDict
from celery import Celery

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

db = SQLAlchemy()
bcrypt = Bcrypt()
login_manager = LoginManager()
migrate = Migrate()
celery = Celery(__name__)

class RedisSession(CallbackDict, SessionMixin):
    def __init__(self, initial=None, sid=None, new=False):
        def on_update(self):
            self.modified = True
        CallbackDict.__init__(self, initial, on_update)
        self.sid = sid
        self.new = new
        self.modified = False

class RedisSessionInterface(SessionInterface):
    serializer = pickle
    session_class = RedisSession

    def __init__(self, redis, prefix='session:'):
        self.redis = redis
        self.prefix = prefix

    def generate_sid(self):
        return str(uuid4())

    def get_redis_expiration_time(self, app, session):
        if session.permanent:
            return app.permanent_session_lifetime
        return timedelta(days=1)

    def get_cookie_name(self, app):
        return app.config.get('SESSION_COOKIE_NAME', 'session')

    def open_session(self, app, request):
        cookie_name = self.get_cookie_name(app)
        sid = request.cookies.get(cookie_name)
        if not sid:
            sid = self.generate_sid()
            return self.session_class(sid=sid, new=True)
        val = self.redis.get(self.prefix + sid)
        if val is not None:
            data = self.serializer.loads(val)
            return self.session_class(data, sid=sid)
        return self.session_class(sid=sid, new=True)

    def save_session(self, app, session, response):
        domain = self.get_cookie_domain(app)
        cookie_name = self.get_cookie_name(app)
        if not session:
            self.redis.delete(self.prefix + session.sid)
            if session.modified:
                response.delete_cookie(cookie_name, domain=domain)
            return
        redis_exp = self.get_redis_expiration_time(app, session)
        cookie_exp = self.get_expiration_time(app, session)
        val = self.serializer.dumps(dict(session))
        self.redis.setex(self.prefix + session.sid, int(redis_exp.total_seconds()), val)
        response.set_cookie(cookie_name, session.sid,
                            expires=cookie_exp, httponly=True,
                            domain=domain)

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
                form_token = request.form.get('csrf_token')
                logger.debug(f"Session CSRF token: {token}")
                logger.debug(f"Form CSRF token: {form_token}")
                if not token or token != form_token:
                    logger.warning("CSRF token mismatch")
                    abort(403)
            return f(*args, **kwargs)
        return decorated_function
    return decorator

def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    app.config['SECRET_KEY'] = secrets.token_hex(32)
    app.config['SERVER_NAME'] = None
    app.config['SESSION_COOKIE_NAME'] = 'streamvault_session'
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)

    db.init_app(app)
    bcrypt.init_app(app)
    login_manager.init_app(app)
    migrate.init_app(app, db)

    redis_client = redis.Redis.from_url(app.config['REDIS_URL'])
    app.session_interface = RedisSessionInterface(redis_client)

    celery.conf.update(app.config)

    @app.context_processor
    def inject_csrf_token():
        return dict(csrf_token=generate_csrf_token())

    from app.auth import auth as auth_blueprint
    app.register_blueprint(auth_blueprint, url_prefix='/auth')
    from app.main import main as main_blueprint
    app.register_blueprint(main_blueprint)

    return app
