FROM python:3.9

WORKDIR /usr/src/app

RUN apt-get update && apt-get install -y netcat-openbsd

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /recordings && \
    mkdir -p /usr/src/app/instance && \
    useradd -m appuser && \
    chown -R appuser:appuser /usr/src/app /recordings

USER appuser

CMD ["sh", "-c", "flask db init || true && flask db migrate || true && flask db upgrade && gunicorn -b 0.0.0.0:5000 'app:create_app()'"]