# Get python 3 image from dockerhub (3.12 matches the local venv the monitor was tested with)
FROM python:3.12

RUN wget -O /usr/local/bin/dumb-init https://github.com/Yelp/dumb-init/releases/download/v1.2.5/dumb-init_1.2.5_x86_64
RUN chmod +x /usr/local/bin/dumb-init

WORKDIR /app

# Install prometheus client, requests, pandas and prophet
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Copy the monitor and its training data into the image
COPY monitor.py shipping_responsetime.json /app/

# Unbuffered output so kubectl logs shows each cycle immediately
ENV PYTHONUNBUFFERED=1

# Port for the Prometheus /metrics endpoint
EXPOSE 8099

# Dumb init
ENTRYPOINT ["/usr/local/bin/dumb-init", "--"]

# Run the application
CMD ["python", "monitor.py"]
