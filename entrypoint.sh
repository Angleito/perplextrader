#!/bin/bash

echo "Current directory: $(pwd)"
echo "Python path: $PYTHONPATH"
echo "Python executable: $(which python)"
echo "Python version: $(python --version)"
echo "Listing installed packages:"
python -m pip list
echo "Checking for Flask:"
python -c "import sys; print(sys.path); try: import flask; print('Flask version:', flask.__version__); except ImportError as e: print('Error importing Flask:', e)"

# Create necessary directories
mkdir -p logs alerts screenshots analysis

# Start cron service
echo "Starting cron service..."
service cron start

# Check if ngrok is enabled
if [ "${USE_NGROK}" = "true" ]; then
    echo "Ngrok is enabled. Setting up ngrok..."
    
    # Check if authtoken is set
    if [ -z "${NGROK_AUTHTOKEN}" ]; then
        echo "ERROR: NGROK_AUTHTOKEN is not set. Ngrok will not work properly."
    else
        echo "Configuring ngrok with authtoken..."
        ngrok config add-authtoken "${NGROK_AUTHTOKEN}"
        
        # Start ngrok in the background if not running in Docker Compose
        if [ -z "${COMPOSE_SERVICE}" ]; then
            echo "Starting ngrok in the background..."
            if [ -n "${NGROK_DOMAIN}" ]; then
                ngrok http --domain="${NGROK_DOMAIN}" "${WEBHOOK_PORT:-5001}" > /app/logs/ngrok.log 2>&1 &
            else
                ngrok http "${WEBHOOK_PORT:-5001}" > /app/logs/ngrok.log 2>&1 &
            fi
        fi
    fi
fi

# Start the main application
echo "Starting the main application..."
exec "$@" 