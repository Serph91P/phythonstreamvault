import os
import traceback
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

def create_app():
    app = Flask(__name__)
    app.config.from_object('config.Config')
    app.logger.info("Starting application...")
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)

    db.init_app(app)
    bcrypt.init_app(app)
    login_manager.init_app(app)
    migrate.init_app(app, db)

    if not os.path.exists(os.path.join(app.config['BASE_DIR'], 'migrations')):
        with app.app_context():
            db.create_all()
            from flask_migrate import init as migrate_init
            migrate_init()
    else:
        app.logger.info("Migrations directory already exists")

    from app.auth import auth as auth_blueprint
    app.register_blueprint(auth_blueprint, url_prefix='/auth')
    
    from app.main import main as main_blueprint
    app.register_blueprint(main_blueprint)

    with app.app_context():
        from .twitch_api import async_init
        async_init(app)

    app.logger.info("Application initialized successfully")
    return app

if __name__ == "__main__":
    app = create_app()
    celery = make_celery(app)
    app.run(host='0.0.0.0', port=5000)