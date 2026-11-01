FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN apt-get update && apt-get install -y --no-install-recommends cron && \
    rm -rf /var/lib/apt/lists/*

COPY crontab /etc/cron.d/coachtracker
RUN chmod 0644 /etc/cron.d/coachtracker && crontab /etc/cron.d/coachtracker

RUN chmod +x entrypoint.sh

EXPOSE 5050

CMD ["./entrypoint.sh"]
