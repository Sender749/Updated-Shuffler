# Use lightweight Python image
FROM python:3.10-slim

# Prevent Python from buffering logs
ENV PYTHONUNBUFFERED=1

# Install system dependencies
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

# Expose port for health checks
EXPOSE 8080

# Bug fix: original CMD used & without sh -c, so Docker passed '&' as a literal
# argument to gunicorn instead of a shell operator — gunicorn would fail.
# Fixed: wrap in sh -c so & is interpreted as a shell background operator.
CMD ["sh", "-c", "gunicorn app:app --bind 0.0.0.0:8080 & python3 main.py"]
