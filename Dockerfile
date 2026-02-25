# Use lightweight Python image
FROM python:3.10-slim

# Prevent Python from buffering logs
ENV PYTHONUNBUFFERED=1

# Install system dependencies (VERY IMPORTANT)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy project files
COPY . /app

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Expose port for Koyeb
EXPOSE 8080

# Start both Flask app and bot
CMD gunicorn app:app --bind 0.0.0.0:8080 & python3 main.py
