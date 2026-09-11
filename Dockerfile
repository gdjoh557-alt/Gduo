FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY group_guard_bot ./group_guard_bot
RUN mkdir -p /app/data

ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "group_guard_bot.bot"]