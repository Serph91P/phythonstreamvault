FROM python:3.11-alpine

WORKDIR /usr/src/app

RUN apk add --no-cache curl bash postgresql-client

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /recordings /usr/src/app/instance

COPY ./entrypoint.sh /usr/src/app/entrypoint.sh
RUN sed -i 's/\r$//' /usr/src/app/entrypoint.sh

RUN adduser -D appuser
RUN chown -R appuser:appuser /usr/src/app /recordings
RUN chmod -R 700 /usr/src/app/instance
RUN chmod 700 /recordings
RUN chmod 755 /usr/src/app/entrypoint.sh

RUN mkdir -p /usr/src/app/instance && \
    chown appuser:appuser /usr/src/app/instance && \
    chmod 700 /usr/src/app/instance

USER appuser

EXPOSE 8000 8080

ENTRYPOINT ["/bin/bash", "/usr/src/app/entrypoint.sh"]
