from twitchAPI.twitch import Twitch
from twitchAPI.eventsub.webhook import EventSubWebhook
from twitchAPI.object.eventsub import ChannelUpdateEvent, StreamOfflineEvent, StreamOnlineEvent
from twitchAPI.helper import first
from app import db
from app.models import Streamer
from flask import current_app
import asyncio

twitch = None
eventsub = None

async def init_twitch(app):
    global twitch, eventsub
    twitch = await Twitch(app.config['TWITCH_CLIENT_ID'], app.config['TWITCH_CLIENT_SECRET'])
    await twitch.authenticate_app([])
    eventsub = EventSubWebhook(
        callback_url=app.config['CALLBACK_URL'],
        port=8080,
        twitch=twitch
    )

async def on_stream_online(data: StreamOnlineEvent):
    app = current_app._get_current_object()
    with app.app_context():
        streamer = Streamer.query.filter_by(twitch_id=data.broadcaster_user_id).first()
        if streamer:
            streamer.is_live = True
            db.session.commit()

async def on_stream_offline(data: StreamOfflineEvent):
    app = current_app._get_current_object()
    with app.app_context():
        streamer = Streamer.query.filter_by(twitch_id=data.broadcaster_user_id).first()
        if streamer:
            streamer.is_live = False
            db.session.commit()

async def on_channel_update(data: ChannelUpdateEvent):
    app = current_app._get_current_object()
    with app.app_context():
        streamer = Streamer.query.filter_by(twitch_id=data.broadcaster_user_id).first()
        if streamer:
            streamer.stream_title = data.title
            streamer.game_name = data.category_name
            db.session.commit()

async def setup_eventsub(app):
    await init_twitch(app)
    # We don't set up global listeners here anymore

async def subscribe_to_events(streamer):
    user = await first(twitch.get_users(logins=[streamer.username]))
    if user:
        streamer.twitch_id = user.id
        db.session.commit()
        
        await eventsub.listen_stream_online(user.id, on_stream_online)
        await eventsub.listen_stream_offline(user.id, on_stream_offline)
        await eventsub.listen_channel_update(user.id, on_channel_update)

def setup_twitch(app):
    if not hasattr(app, 'twitch_setup_done'):
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(setup_eventsub(app))
        else:
            loop.run_until_complete(setup_eventsub(app))
        app.twitch_setup_done = True


def add_streamer(username, user_id):
    streamer = Streamer(username=username, user_id=user_id)
    db.session.add(streamer)
    db.session.commit()
    
    async def async_subscribe():
        await subscribe_to_events(streamer)

    loop = asyncio.get_event_loop()
    if loop.is_running():
        loop.create_task(async_subscribe())
    else:
        loop.run_until_complete(async_subscribe())