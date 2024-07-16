from app import db, create_app
from app.models import User
import flask_migrate
import os

def init_db():
    app = create_app()
    with app.app_context():
        if not os.path.exists(os.path.join(app.root_path, 'migrations')):
            flask_migrate.init()
            flask_migrate.stamp()
        flask_migrate.upgrade()
        if not User.query.first():
            print("No users found. The setup page will be available.")
        else:
            print("Users found in the database.")

if __name__ == '__main__':
    init_db()
