FROM python:3.11-alpine

WORKDIR /usr/src/app

RUN apk add --no-cache curl bash

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /recordings /usr/src/app/instance

COPY ./entrypoint.sh /usr/src/app/entrypoint.sh
RUN chmod +x /usr/src/app/entrypoint.sh && \
    sed -i 's/\r$//' /usr/src/app/entrypoint.sh

RUN adduser -D appuser && \
    chown -R appuser:appuser /usr/src/app /recordings

USER appuser

EXPOSE 8000 8080

ENTRYPOINT ["/bin/sh", "-c", "/usr/src/app/entrypoint.sh"]


