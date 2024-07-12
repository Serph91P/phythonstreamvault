from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, abort, current_app
from flask_login import login_required, current_user
from app import db
from app.models import Streamer, User
from app.twitch_api import async_init

main = Blueprint('main', __name__)

@main.route('/')
def index():
    if not User.query.first():
        return redirect(url_for('auth.setup'))
    if not current_user.is_authenticated:
        return redirect(url_for('auth.login'))
    return render_template('index.html', title='Home')

@main.route('/dashboard')
@login_required
def dashboard():
    streamers = Streamer.query.filter_by(user_id=current_user.id).all()
    return render_template('dashboard.html', title='Dashboard', streamers=streamers)

@main.route('/health')
def health_check():
    return jsonify({"status": "healthy"}), 200
