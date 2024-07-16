FROM python:3.9

WORKDIR /usr/src/app

RUN apt-get update && apt-get install -y netcat-openbsd curl

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /recordings && \
    mkdir -p /usr/src/app/instance

COPY entrypoint.sh /usr/src/app/entrypoint.sh
RUN chmod +x /usr/src/app/entrypoint.sh

RUN useradd -m appuser && \
    chown -R appuser:appuser /usr/src/app /recordings

USER appuser

EXPOSE 8000 8080

ENTRYPOINT ["/usr/src/app/entrypoint.sh"]

