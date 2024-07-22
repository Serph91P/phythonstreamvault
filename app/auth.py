from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_user, logout_user, login_required, current_user
from app.models import User
from app import db, bcrypt, csrf_protect
import logging

logger = logging.getLogger(__name__)

auth = Blueprint('auth', __name__)

@auth.route('/login', methods=['GET', 'POST'])
@csrf_protect()
def login():
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        remember = request.form.get('remember')
        user = User.query.filter_by(email=email).first()
        if user and bcrypt.check_password_hash(user.password, password):
            login_user(user, remember=bool(remember))
            return redirect(url_for('main.index'))
        else:
            flash('Login unsuccessful. Please check email and password', 'danger')
    return render_template('login.html', title='Login')

@auth.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('main.index'))

@auth.route('/setup', methods=['GET', 'POST'])
@csrf_protect()
def setup():
    logger.debug(f"Request method: {request.method}")
    logger.debug(f"Form data: {request.form}")
    if User.query.first():
        flash('Setup has already been completed.', 'warning')
        return redirect(url_for('main.index'))
    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email')
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        if password != confirm_password:
            flash('Passwords do not match', 'danger')
        else:
            try:
                hashed_password = bcrypt.generate_password_hash(password).decode('utf-8')
                user = User(username=username, email=email, password=hashed_password)
                db.session.add(user)
                db.session.commit()
                login_user(user)
                flash('Admin account created successfully. You are now logged in.', 'success')
                return redirect(url_for('main.index'))
            except Exception as e:
                db.session.rollback()
                flash(f'An error occurred: {str(e)}', 'danger')
    return render_template('auth/setup.html', title='Setup')
