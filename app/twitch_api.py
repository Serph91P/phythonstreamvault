import asyncio
import traceback
from config import Config
from flask import current_app
from twitchAPI.twitch import Twitch
from twitchAPI.eventsub.webhook import EventSubWebhook
from twitchAPI.object.eventsub import ChannelFollowEvent
from contextlib import asynccontextmanager
import redis
from redis.exceptions import LockError

class TwitchAPI:
    def __init__(self):
        self.twitch = None
        self.eventsub = None

    async def get_eventsub_subscriptions(self):
        subscriptions = await self.twitch.get_eventsub_subscriptions()
        return [sub.to_dict() for sub in subscriptions]

    async def delete_all_subscriptions(self):
        subscriptions = await self.twitch.get_eventsub_subscriptions()
        for sub in subscriptions:
            await self.twitch.delete_eventsub_subscription(sub.id)
        return {"message": "All subscriptions deleted successfully"}

    async def subscribe_to_stream_online(self, broadcaster_id):
        try:
            await self.eventsub.listen_stream_online(broadcaster_id, self.on_stream_online)
            return {"message": f"Subscribed to stream online events for broadcaster {broadcaster_id}"}
        except Exception as e:
            return {"error": f"Failed to subscribe: {str(e)}"}

    async def on_stream_online(self, data):
        print(f"Stream online: {data}")
        # Add your logic here to handle the stream online event

    async def subscribe_to_channel_follow(self, broadcaster_id):
        try:
            await self.eventsub.listen_channel_follow_v2(broadcaster_id, self.on_channel_follow)
            return {"message": f"Subscribed to channel follow events for broadcaster {broadcaster_id}"}
        except Exception as e:
            return {"error": f"Failed to subscribe: {str(e)}"}

    async def on_channel_follow(self, data: ChannelFollowEvent):
        print(f"New follower: {data.user_name} followed {data.broadcaster_user_name}")
        # Add your logic here to handle the channel follow event

@asynccontextmanager
async def manage_async_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        yield loop
    finally:
        loop.close()

async def setup_twitch(app):
    app.logger.info("Entering setup_twitch")
    try:
        client_id = app.config['TWITCH_CLIENT_ID']
        client_secret = app.config['TWITCH_CLIENT_SECRET']
        app.logger.info(f"Initializing Twitch API with CLIENT_ID: {client_id}")
        twitch = await Twitch(client_id, client_secret)
        await twitch.authenticate_app([])
        app.logger.info("Twitch API authenticated successfully")
        return twitch
    except Exception as e:
        app.logger.error(f"Failed to initialize Twitch API: {str(e)}")
        app.logger.error(traceback.format_exc())
        return None

async def setup_eventsub(app, twitch_instance):
    app.logger.info("Entering setup_eventsub")
    try:
        if not twitch_instance:
            app.logger.error("Twitch instance is None, cannot setup EventSub")
            return None

        callback_url = app.config['CALLBACK_URL']
        port = Config.get_eventsub_webhook_port()
        secret = app.config['TWITCH_WEBHOOK_SECRET']
        
        app.logger.info(f"Full callback URL: {callback_url}")
        app.logger.info(f"Initializing EventSubWebhook with CALLBACK_URL: {callback_url}, PORT: {port}")
        eventsub = EventSubWebhook(callback_url, port, secret, twitch_instance)
        
        if asyncio.iscoroutinefunction(eventsub.start):
            await eventsub.start()
        else:
            eventsub.start()
        
        app.logger.info("EventSubWebhook started successfully")
        return eventsub
    except Exception as e:
        app.logger.error(f"Failed to setup EventSub: {str(e)}")
        app.logger.error(traceback.format_exc())
        return None

def init_twitch_api(app):
    required_configs = ['TWITCH_CLIENT_ID', 'TWITCH_CLIENT_SECRET', 'TWITCH_WEBHOOK_SECRET', 'CALLBACK_URL']
    missing_configs = [config for config in required_configs if not app.config.get(config)]
    
    app.config['EVENTSUB_WEBHOOK_PORT'] = Config.get_eventsub_webhook_port()
    
    if missing_configs:
        app.logger.error(f"Missing required configuration values: {', '.join(missing_configs)}")
        return None

    twitch_api = TwitchAPI()
    
    async def initialize():
        twitch_api.twitch = await setup_twitch(app)
        if twitch_api.twitch:
            twitch_api.eventsub = await setup_eventsub(app, twitch_api.twitch)

    asyncio.run(initialize())
    return twitch_api

async def setup_redis(app):
    app.logger.info("Entering setup_redis")
    try:
        redis_url = app.config['REDIS_URL']
        app.logger.info(f"Initializing Redis with REDIS_URL: {redis_url}")
        redis_client = redis.from_url(redis_url)
        await redis_client.ping()
        app.logger.info("Redis connected successfully")
        return redis_client
    except Exception as e:
        app.logger.error(f"Failed to initialize Redis: {str(e)}")
        app.logger.error(traceback.format_exc())
        return None

    # async def get_eventsub_subscriptions(self):
    #     subscriptions = await self.twitch.get_eventsub_subscriptions()
    #     return [sub.to_dict() for sub in subscriptions]

    # async def delete_all_subscriptions(self):
    #     subscriptions = await self.twitch.get_eventsub_subscriptions()
    #     for sub in subscriptions:
    #         await self.twitch.delete_eventsub_subscription(sub.id)
    #     return {"message": "All subscriptions deleted successfully"}

    # async def subscribe_to_stream_online(self, broadcaster_id):
    #     try:
    #         await self.eventsub.listen_stream_online(broadcaster_id, self.on_stream_online)
    #         return {"message": f"Subscribed to stream online events for broadcaster {broadcaster_id}"}
    #     except Exception as e:
    #         return {"error": f"Failed to subscribe: {str(e)}"}

    # async def on_stream_online(self, data):
    #     print(f"Stream online: {data}")
        # Add your logic here to handle the stream online event
