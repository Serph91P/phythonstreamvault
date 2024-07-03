from app import create_app, db, login_manager
from app.tasks import start_recording_task
from app.models import User
from flask import jsonify
from app.auth import auth as auth_blueprint
from app.main import main as main_blueprint

app = create_app()

login_manager.init_app(app)

@app.route('/health')
def health():
    return jsonify({"status": "healthy"}), 200

@app.before_first_request
def create_tables():
    db.create_all()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
