from datetime import datetime
from . import db, login_manager
from flask_login import UserMixin

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(20), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(60), nullable=False)
    
    streamers = db.relationship('Streamer', back_populates='user', lazy=True)

class Streamer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    twitch_id = db.Column(db.String(50), unique=True)
    is_live = db.Column(db.Boolean, default=False)
    stream_title = db.Column(db.String(140))
    game_name = db.Column(db.String(100))
    last_updated = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', back_populates='streamers')

class TwitchEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    event_type = db.Column(db.String(80), nullable=False)
    event_data = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<TwitchEvent {self.event_type}>'
