from flask import Blueprint, render_template, redirect, url_for, flash, request, session
from app import db, bcrypt, csrf_protect
from app.forms import LoginForm, SetupForm
from app.models import User
from flask_login import login_user, logout_user, login_required, current_user
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, SubmitField
from wtforms.validators import DataRequired, Email, Length

class SetupForm(FlaskForm):
    username = StringField('Username', validators=[DataRequired(), Length(min=2, max=20)])
    email = StringField('Email', validators=[DataRequired(), Email()])
    password = PasswordField('Password', validators=[DataRequired()])
    submit = SubmitField('Set Up')


auth = Blueprint('auth', __name__)

@auth.route('/login', methods=['GET', 'POST'])
@csrf_protect()
def login():
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email).first()
        if user and bcrypt.check_password_hash(user.password, form.password):
            login_user(user, remember=form.remember)
            return redirect(url_for('main.index'))
        else:
            flash('Login unsuccessful. Please check email and password', 'danger')
    return render_template('login.html', title='Login', form=form)

@auth.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('main.index'))

@auth.route('/setup', methods=['GET', 'POST'])
@csrf_protect()
def setup():
    if User.query.first():
        flash('Setup has already been completed.', 'warning')
        return redirect(url_for('main.index'))
    form = SetupForm()
    if form.validate_on_submit():
        try:
            hashed_password = bcrypt.generate_password_hash(form.password).decode('utf-8')
            user = User(username=form.username, email=form.email, password=hashed_password)
            db.session.add(user)
            db.session.commit()
            login_user(user)
            flash('Admin account created successfully. You are now logged in.', 'success')
            return redirect(url_for('main.index'))
        except Exception as e:
            db.session.rollback()
            flash(f'An error occurred: {str(e)}', 'danger')
    return render_template('auth/setup.html', title='Setup', form=form)
