# One worker: the job table, the file token map and the limiter storage are per
# process, so the worker count must stay at one (gunicorn otherwise reads WEB_CONCURRENCY).
web: gunicorn frc_cam_gui_app:app --bind 0.0.0.0:$PORT --workers 1 --worker-class gthread --threads 16
