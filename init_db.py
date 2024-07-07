from app import db, create_app
from app.models import User

def init_db():
    app = create_app()
    with app.app_context():
        db.create_all()
        if not User.query.first():
            print("No users found. The setup page will be available.")
        else:
            print("Users found in the database.")

if __name__ == '__main__':
    init_db()