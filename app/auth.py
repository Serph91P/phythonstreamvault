from flask import Blueprint, render_template, redirect, url_for, flash, request, make_response
from app import db, bcrypt, csrf_logger
from app.forms import LoginForm, SetupForm
from app.models import User
from flask_login import login_user, logout_user, login_required, current_user
from flask_wtf.csrf import CSRFError

auth = Blueprint('auth', __name__)

@auth.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))
    form = LoginForm()
    if form.validate_on_submit():
        csrf_logger.debug(f"Form CSRF token: {form.csrf_token.data}")
        csrf_logger.debug(f"Session CSRF token: {session.get('csrf_token')}")
        csrf_logger.debug(f"Cookie CSRF token: {request.cookies.get('csrf_token')}")
        user = User.query.filter_by(email=form.email.data).first()
        if user and bcrypt.check_password_hash(user.password, form.password.data):
            login_user(user, remember=form.remember.data)
            response = make_response(redirect(url_for('main.index')))
            response.set_cookie('csrf_token', request.cookies.get('csrf_token'))
            return response
        else:
            flash('Login unsuccessful. Please check email and password', 'danger')
    return render_template('login.html', title='Login', form=form)


@auth.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('main.index'))

@auth.route('/setup', methods=['GET', 'POST'])
def setup():
    if User.query.first():
        flash('Setup has already been completed.', 'warning')
        return redirect(url_for('main.index'))
    form = SetupForm()
    if form.validate_on_submit():
        try:
            hashed_password = bcrypt.generate_password_hash(form.password.data).decode('utf-8')
            user = User(username=form.username.data, email=form.email.data, password=hashed_password)
            db.session.add(user)
            db.session.commit()
            login_user(user)
            flash('Admin account created successfully. You are now logged in.', 'success')
            return redirect(url_for('main.index'))
        except Exception as e:
            db.session.rollback()
            flash(f'An error occurred: {str(e)}', 'danger')
    return render_template('auth/setup.html', title='Setup', form=form)

@auth.errorhandler(CSRFError)
def handle_csrf_error(e):
    return render_template('csrf_error.html', reason=e.description), 400