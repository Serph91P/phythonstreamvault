import subprocess
from app.celery import celery 

@celery.task
def start_recording_task(streamer_id):
    stream_url = f'https://twitch.tv/{streamer_id}'
    output_file = f'/recordings/{streamer_id}.mp4'
    subprocess.run(['streamlink', stream_url, 'best', '-o', output_file])