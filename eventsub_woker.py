from app import create_app
from app.twitch_api import setup_eventsub

app = create_app()
with app.app_context():
    eventsub = setup_eventsub(app, app.config['TWITCH_API'])
    eventsub.start()
