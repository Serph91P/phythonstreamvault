from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from . import db
from .models import Streamer

main = Blueprint('main', __name__)

@main.route('/')
def index():
    return render_template('index.html', title='Home')

@main.route('/dashboard')
@login_required
def dashboard():
    streamers = Streamer.query.filter_by(user_id=current_user.id).all()
    return render_template('dashboard.html', title='Dashboard', streamers=streamers)

@main.route('/add_streamer', methods=['POST'])
@login_required
def add_streamer():
    username = request.form.get('username')
    if username:
        streamer = Streamer(username=username, user_id=current_user.id)
        db.session.add(streamer)
        db.session.commit()
        flash('Streamer added successfully', 'success')
    return redirect(url_for('main.dashboard'))
