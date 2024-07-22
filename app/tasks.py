from app import celery, db
from app.models import Streamer, Stream
from flask import current_app
from twitchAPI.object.eventsub import StreamOnlineEvent, StreamOfflineEvent, ChannelUpdateEvent

@celery.task
def add_streamer_task(username, user_id):
    current_app.logger.info(f"Starting add_streamer_task for {username}")
    twitch = current_app.config['TWITCH_API']
    eventsub = current_app.config['EVENTSUB']

    try:
        user_info = twitch.get_users(logins=[username])
        if not user_info['data']:
            current_app.logger.warning(f"Streamer {username} not found")
            return {'status': 'error', 'message': 'Streamer not found'}

        streamer_id = user_info['data'][0]['id']
        streamer = Streamer(id=streamer_id, username=username, user_id=user_id)
        db.session.add(streamer)
        db.session.commit()

        # Subscribe to events
        eventsub.listen_stream_online(streamer_id, on_stream_online)
        eventsub.listen_stream_offline(streamer_id, on_stream_offline)
        eventsub.listen_channel_update(streamer_id, on_channel_update)

        current_app.logger.info(f"Streamer {username} added successfully")
        return {'status': 'success', 'message': f'Streamer {username} added successfully'}
    except Exception as e:
        current_app.logger.error(f"Error adding streamer {username}: {str(e)}")
        db.session.rollback()
        return {'status': 'error', 'message': str(e)}
    
@celery.task
def resubscribe_all_streamers():
    current_app.logger.info("Starting resubscribe_all_streamers task")
    twitch = current_app.config['TWITCH_API']
    eventsub = current_app.config['EVENTSUB']

    try:
        streamers = Streamer.query.all()
        for streamer in streamers:
            eventsub.listen_stream_online(streamer.id, on_stream_online)
            eventsub.listen_stream_offline(streamer.id, on_stream_offline)
            eventsub.listen_channel_update(streamer.id, on_channel_update)
        current_app.logger.info("Resubscribed to all streamers successfully")
        return {'status': 'success', 'message': 'Resubscribed to all streamers'}
    except Exception as e:
        current_app.logger.error(f"Error resubscribing to streamers: {str(e)}")
        return {'status': 'error', 'message': str(e)}

def on_stream_online(data: StreamOnlineEvent):
    current_app.logger.info(f"Stream online event received for {data.broadcaster_user_name}")
    streamer = Streamer.query.get(data.broadcaster_user_id)
    if streamer:
        stream = Stream(
            id=data.id,
            streamer_id=streamer.id,
            started_at=data.started_at,
            type=data.type
        )
        db.session.add(stream)
        db.session.commit()

def on_stream_offline(data: StreamOfflineEvent):
    current_app.logger.info(f"Stream offline event received for {data.broadcaster_user_name}")
    stream = Stream.query.filter_by(streamer_id=data.broadcaster_user_id, ended_at=None).first()
    if stream:
        stream.ended_at = data.ended_at
        db.session.commit()

def on_channel_update(data: ChannelUpdateEvent):
    current_app.logger.info(f"Channel update event received for {data.broadcaster_user_name}")
    streamer = Streamer.query.get(data.broadcaster_user_id)
    if streamer:
        streamer.title = data.title
        streamer.category_id = data.category_id
        streamer.category_name = data.category_name
        db.session.commit()
