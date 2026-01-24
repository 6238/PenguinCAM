# Use official Python image from Docker Hub (no GitHub downloads)
FROM python:3.11-slim

WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Expose port (Railway sets PORT env var)
EXPOSE $PORT

# Start gunicorn
CMD gunicorn frc_cam_gui_app:app --bind 0.0.0.0:$PORT
