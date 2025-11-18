#!/bin/bash

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# AlgoMirror Installation Banner
echo -e "${BLUE}"
echo "  █████╗ ██╗      ██████╗  ██████╗ ███╗   ███╗██╗██████╗ ██████╗  ██████╗ ██████╗ "
echo " ██╔══██╗██║     ██╔════╝ ██╔═══██╗████╗ ████║██║██╔══██╗██╔══██╗██╔═══██╗██╔══██╗"
echo " ███████║██║     ██║  ███╗██║   ██║██╔████╔██║██║██████╔╝██████╔╝██║   ██║██████╔╝"
echo " ██╔══██║██║     ██║   ██║██║   ██║██║╚██╔╝██║██║██╔══██╗██╔══██╗██║   ██║██╔══██╗"
echo " ██║  ██║███████╗╚██████╔╝╚██████╔╝██║ ╚═╝ ██║██║██║  ██║██║  ██║╚██████╔╝██║  ██║"
echo " ╚═╝  ╚═╝╚══════╝ ╚═════╝  ╚═════╝ ╚═╝     ╚═╝╚═╝╚═╝  ╚═╝╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═╝"
echo "                      Multi-Account Management Platform                            "
echo -e "${NC}"

# Create logs directory
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
LOGS_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOGS_DIR"

# Generate unique log file
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG_FILE="$LOGS_DIR/install_${TIMESTAMP}.log"

# Function to log messages
log_message() {
    local message="$1"
    local color="$2"
    echo -e "${color}${message}${NC}" | tee -a "$LOG_FILE"
}

# Function to check command status
check_status() {
    if [ $? -ne 0 ]; then
        log_message "Error: $1" "$RED"
        exit 1
    fi
}

# Function to generate random hex string
generate_hex() {
    python3 -c "import secrets; print(secrets.token_hex(32))"
}

# Function to generate Fernet key
generate_fernet_key() {
    python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
}

# Function to check timezone
check_timezone() {
    current_tz=$(timedatectl | grep "Time zone" | awk '{print $3}')
    log_message "Current timezone: $current_tz" "$BLUE"

    if [[ "$current_tz" == "Asia/Kolkata" ]]; then
        log_message "Server is already set to IST timezone." "$GREEN"
        return 0
    fi

    log_message "Server is not set to IST timezone." "$YELLOW"
    read -p "Would you like to change the timezone to IST? (y/n): " change_tz
    if [[ $change_tz =~ ^[Yy]$ ]]; then
        log_message "Changing timezone to IST..." "$BLUE"
        sudo timedatectl set-timezone Asia/Kolkata
        check_status "Failed to change timezone"
        log_message "Timezone successfully changed to IST" "$GREEN"
    else
        log_message "Keeping current timezone: $current_tz" "$YELLOW"
    fi
}

# Function to handle existing files/directories
handle_existing() {
    local path=$1
    local type=$2
    local name=$3

    if [ -e "$path" ]; then
        log_message "Warning: $name already exists at $path" "$YELLOW"
        read -p "Would you like to backup the existing $type? (y/n): " backup_choice
        if [[ $backup_choice =~ ^[Yy]$ ]]; then
            backup_path="${path}_backup_$(date +%Y%m%d_%H%M%S)"
            log_message "Creating backup at $backup_path" "$BLUE"
            sudo mv "$path" "$backup_path"
            check_status "Failed to create backup of $name"
            return 0
        else
            read -p "Would you like to remove the existing $type? (y/n): " remove_choice
            if [[ $remove_choice =~ ^[Yy]$ ]]; then
                log_message "Removing existing $type..." "$BLUE"
                if [ -d "$path" ]; then
                    sudo rm -rf "$path"
                else
                    sudo rm -f "$path"
                fi
                check_status "Failed to remove existing $type"
                return 0
            else
                log_message "Installation cannot proceed without handling existing $type" "$RED"
                exit 1
            fi
        fi
    fi
    return 0
}

# Start logging
log_message "Starting AlgoMirror installation" "$BLUE"
log_message "Log file: $LOG_FILE" "$BLUE"
log_message "========================================" "$BLUE"

# Check timezone
check_timezone

# Collect installation parameters
log_message "\n=== AlgoMirror Installation Configuration ===" "$YELLOW"
log_message "========================================" "$BLUE"

# Get domain name
while true; do
    read -p "Enter your domain name (e.g., algomirror.yourdomain.com): " DOMAIN
    if [ -z "$DOMAIN" ]; then
        log_message "Error: Domain name is required" "$RED"
        continue
    fi
    # Domain validation - must contain at least one dot
    if [[ ! $DOMAIN =~ ^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?(\.([a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?))+$ ]]; then
        log_message "Error: Invalid domain format (e.g., algomirror.yourdomain.com)" "$RED"
        continue
    fi

    # Check if it's a subdomain
    if [[ $DOMAIN =~ ^[^.]+\.[^.]+\.[^.]+$ ]]; then
        IS_SUBDOMAIN=true
    else
        IS_SUBDOMAIN=false
    fi
    break
done

# Ask for default OpenAlgo instance (optional)
log_message "\n=== Default OpenAlgo Configuration (Optional) ===" "$YELLOW"
log_message "You can configure this later in the web interface" "$BLUE"
echo ""

read -p "Enter default OpenAlgo host URL (or press Enter to skip) [http://127.0.0.1:5000]: " OPENALGO_HOST
OPENALGO_HOST=${OPENALGO_HOST:-http://127.0.0.1:5000}

read -p "Enter default OpenAlgo WebSocket URL (or press Enter to skip) [ws://127.0.0.1:8765]: " OPENALGO_WS
OPENALGO_WS=${OPENALGO_WS:-ws://127.0.0.1:8765}

# Installation paths
BASE_PATH="/var/python/algomirror"
ALGOMIRROR_PATH="$BASE_PATH/app"
VENV_PATH="$BASE_PATH/venv"
SOCKET_PATH="$BASE_PATH"
SOCKET_FILE="$SOCKET_PATH/algomirror.sock"
SERVICE_NAME="algomirror"
APP_PORT=8000

log_message "\n=== Starting AlgoMirror installation ===" "$YELLOW"

# Update system packages
log_message "\nUpdating system packages..." "$BLUE"
sudo apt-get update && sudo apt-get upgrade -y
check_status "Failed to update system packages"

# Install required packages
log_message "\nInstalling required packages..." "$BLUE"
sudo apt-get install -y python3 python3-venv python3-pip python3-full nginx git software-properties-common snapd ufw certbot python3-certbot-nginx nodejs npm
check_status "Failed to install required packages"

# Install uv package installer
log_message "\nInstalling uv package installer..." "$BLUE"
# Try snap first, then pip fallback
if command -v snap >/dev/null 2>&1; then
    if [ ! -e /snap ] && [ -d /var/lib/snapd/snap ]; then
        sudo ln -s /var/lib/snapd/snap /snap
    fi
    sleep 2
    if sudo snap install astral-uv --classic 2>/dev/null; then
        log_message "uv installed via snap" "$GREEN"
    else
        log_message "Snap installation failed, using pip fallback" "$YELLOW"
        sudo python3 -m pip install uv
    fi
else
    sudo python3 -m pip install uv
fi
check_status "Failed to install uv"

# Configure firewall
log_message "\nConfiguring firewall..." "$BLUE"
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow ssh
sudo ufw allow 'Nginx Full'
sudo ufw --force enable
check_status "Failed to configure firewall"

# Check and handle existing installation
handle_existing "$BASE_PATH" "installation directory" "AlgoMirror directory"

# Create base directory
log_message "\nCreating base directory..." "$BLUE"
sudo mkdir -p $BASE_PATH
check_status "Failed to create base directory"

# Clone repository
log_message "\nCloning AlgoMirror repository..." "$BLUE"
# Note: Update this URL with actual repository URL when available
sudo git clone https://github.com/marketcalls/algomirror.git $ALGOMIRROR_PATH 2>/dev/null || {
    log_message "Note: Repository clone failed. Manual file upload required." "$YELLOW"
    # If repository not available, create directory structure
    sudo mkdir -p $ALGOMIRROR_PATH
    log_message "Please upload AlgoMirror files to: $ALGOMIRROR_PATH" "$YELLOW"
    log_message "Use: scp -r /path/to/algomirror/* root@server:$ALGOMIRROR_PATH/" "$BLUE"
    read -p "Press Enter after uploading files to continue..."

    # Verify critical files exist
    if [ ! -f "$ALGOMIRROR_PATH/wsgi.py" ]; then
        log_message "Error: wsgi.py not found. Please upload all application files." "$RED"
        exit 1
    fi
    if [ ! -f "$ALGOMIRROR_PATH/init_db.py" ]; then
        log_message "Error: init_db.py not found. Please upload all application files." "$RED"
        exit 1
    fi
    if [ ! -d "$ALGOMIRROR_PATH/app" ]; then
        log_message "Error: app directory not found. Please upload all application files." "$RED"
        exit 1
    fi
    log_message "Application files verified successfully!" "$GREEN"
}

# Create virtual environment using uv
log_message "\nSetting up Python virtual environment with uv..." "$BLUE"
if [ -d "$VENV_PATH" ]; then
    log_message "Warning: Virtual environment already exists, removing..." "$YELLOW"
    sudo rm -rf "$VENV_PATH"
fi
# Create directory if it doesn't exist
sudo mkdir -p $(dirname $VENV_PATH)

# Detect how uv is installed and set the appropriate command
if command -v uv >/dev/null 2>&1; then
    # uv is available as a standalone command (snap or astral installer)
    UV_CMD="uv"
    log_message "Using standalone uv command" "$GREEN"
elif python3 -m uv --version >/dev/null 2>&1; then
    # uv is available as a Python module
    UV_CMD="python3 -m uv"
    log_message "Using uv as Python module" "$GREEN"
else
    log_message "Error: uv is not available" "$RED"
    exit 1
fi

# Create virtual environment using uv
sudo $UV_CMD venv $VENV_PATH
check_status "Failed to create virtual environment with uv"

# Install Python dependencies using uv (faster installation)
log_message "\nInstalling Python dependencies with uv..." "$BLUE"

# Create a clean requirements file
cat > /tmp/algomirror_requirements.txt << 'EOF'
alembic==1.16.4
anyio==4.10.0
APScheduler==3.11.0
blinker==1.9.0
build==1.3.0
cachelib==0.13.0
cachetools==6.2.0
certifi==2025.8.3
cffi==1.17.1
click==8.2.1
colorama==0.4.6
cryptography==45.0.6
Cython==3.1.4
Deprecated==1.2.18
dnspython==2.7.0
email_validator==2.2.0
Flask==3.1.2
flask-cors==6.0.1
Flask-Limiter==3.12
Flask-Login==0.6.3
Flask-Migrate==4.1.0
Flask-Session==0.8.0
Flask-SQLAlchemy==3.1.1
flask-talisman==1.1.0
Flask-WTF==1.2.2
greenlet==3.2.4
h11==0.16.0
httpcore==1.0.9
httpx==0.28.1
idna==3.10
itsdangerous==2.2.0
Jinja2==3.1.6
limits==5.5.0
llvmlite==0.44.0
Mako==1.3.10
markdown-it-py==4.0.0
MarkupSafe==3.0.2
mdurl==0.1.2
msgspec==0.19.0
numba==0.61.2
numpy==2.2.6
openalgo==1.0.30
ordered-set==4.1.0
packaging==25.0
pandas==2.3.2
pycparser==2.22
Pygments==2.19.2
pyproject-hooks==1.2.0
python-dateutil==2.9.0.post0
python-dotenv==1.1.1
python-json-logger==3.3.0
pytz==2025.2
redis==6.4.0
rich==13.9.4
six==1.17.0
sniffio==1.3.1
SQLAlchemy==2.0.43
ta-lib==0.6.7
typing_extensions==4.15.0
tzdata==2025.2
tzlocal==5.3.1
websocket-client==1.8.0
Werkzeug==3.1.3
wrapt==1.17.3
WTForms==3.2.1
gunicorn==23.0.0
eventlet==0.38.1
EOF

# Install dependencies using uv
sudo $UV_CMD pip install --python $VENV_PATH/bin/python -r /tmp/algomirror_requirements.txt
check_status "Failed to install Python dependencies"

# Verify gunicorn and eventlet installation
log_message "\nVerifying gunicorn and eventlet installation..." "$BLUE"
ACTIVATE_CMD="source $VENV_PATH/bin/activate"
if ! sudo bash -c "$ACTIVATE_CMD && pip freeze | grep -q 'gunicorn=='"; then
    log_message "Installing gunicorn..." "$YELLOW"
    sudo $UV_CMD pip install --python $VENV_PATH/bin/python gunicorn
    check_status "Failed to install gunicorn"
fi
if ! sudo bash -c "$ACTIVATE_CMD && pip freeze | grep -q 'eventlet=='"; then
    log_message "Installing eventlet..." "$YELLOW"
    sudo $UV_CMD pip install --python $VENV_PATH/bin/python eventlet
    check_status "Failed to install eventlet"
fi

# Install Node dependencies and build CSS
if [ -f "$ALGOMIRROR_PATH/package.json" ]; then
    log_message "\nInstalling Node dependencies and building CSS..." "$BLUE"
    cd $ALGOMIRROR_PATH
    sudo npm install
    sudo npm run build-css
fi

# Generate security keys
log_message "\nGenerating security keys..." "$BLUE"
SECRET_KEY=$(generate_hex)
ENCRYPTION_KEY=$(generate_fernet_key)

# Configure .env file
log_message "\nConfiguring environment file..." "$BLUE"
ENV_FILE="$ALGOMIRROR_PATH/.env"
handle_existing "$ENV_FILE" "environment file" ".env file"

sudo tee $ENV_FILE > /dev/null << EOL
# AlgoMirror Environment Configuration
# Generated: $(date)

# Flask Configuration
FLASK_APP=app.py
FLASK_ENV=production
SECRET_KEY='$SECRET_KEY'

# Database Configuration
DATABASE_URL=sqlite:///$BASE_PATH/instance/algomirror.db

# Redis Configuration (optional - uses memory if not provided)
# For production, uncomment and configure Redis
# REDIS_URL=redis://localhost:6379/0

# Encryption Configuration
ENCRYPTION_KEY='$ENCRYPTION_KEY'

# Logging Configuration
LOG_LEVEL=INFO

# Session Configuration
SESSION_TYPE=filesystem

# CORS Configuration
CORS_ORIGINS=https://$DOMAIN

# OpenAlgo Default Configuration
DEFAULT_OPENALGO_HOST=$OPENALGO_HOST
DEFAULT_OPENALGO_WS=$OPENALGO_WS

# Ping Monitoring Configuration
PING_MONITORING_INTERVAL=30
PING_MONITORING_ENABLED=true
PING_MAX_FAILURES=3
PING_QUIET_MODE=false

# Production Security Settings
WTF_CSRF_SSL_STRICT=True
SESSION_COOKIE_SECURE=True
SESSION_COOKIE_HTTPONLY=True
SESSION_COOKIE_SAMESITE='Lax'

# AlgoMirror runs on port 8000 internally
ALGOMIRROR_PORT=$APP_PORT
EOL

check_status "Failed to configure environment file"

# Create required directories
log_message "\nCreating required directories..." "$BLUE"
sudo mkdir -p $BASE_PATH/instance
sudo mkdir -p $BASE_PATH/logs
sudo mkdir -p $ALGOMIRROR_PATH/flask_session
sudo mkdir -p $ALGOMIRROR_PATH/logs
log_message "Created: instance, logs, flask_session directories" "$GREEN"

# Initialize database
log_message "\nInitializing database..." "$BLUE"
if [ -f "$ALGOMIRROR_PATH/init_db.py" ]; then
    cd $ALGOMIRROR_PATH
    sudo bash -c "$ACTIVATE_CMD && python init_db.py"
    check_status "Failed to initialize database"

    # Verify database was created
    if [ -f "$BASE_PATH/instance/algomirror.db" ]; then
        log_message "Database initialized successfully at $BASE_PATH/instance/algomirror.db" "$GREEN"
    else
        log_message "Warning: Database file not found after initialization" "$YELLOW"
    fi
else
    log_message "Error: init_db.py not found, cannot initialize database" "$RED"
    exit 1
fi

# Set correct permissions
log_message "\nSetting permissions..." "$BLUE"
sudo chown -R www-data:www-data $BASE_PATH
check_status "Failed to change ownership"
sudo chmod -R 755 $BASE_PATH
check_status "Failed to set permissions"

# Remove socket file if it exists
if [ -S "$SOCKET_FILE" ]; then
    sudo rm -f $SOCKET_FILE
fi

# Check and handle existing Nginx configuration
handle_existing "/etc/nginx/sites-available/$DOMAIN" "Nginx configuration" "Nginx config file"

# Configure initial Nginx for SSL
log_message "\nConfiguring initial Nginx setup..." "$BLUE"
sudo tee /etc/nginx/sites-available/$DOMAIN > /dev/null << EOL
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;
    root /var/www/html;

    location / {
        try_files \$uri \$uri/ =404;
    }
}
EOL

# Enable site and remove default
sudo rm -f /etc/nginx/sites-enabled/default
sudo ln -sf /etc/nginx/sites-available/$DOMAIN /etc/nginx/sites-enabled/
check_status "Failed to enable Nginx site"

# Reload Nginx
log_message "\nTesting and reloading Nginx..." "$BLUE"
sudo nginx -t && sudo systemctl reload nginx
check_status "Failed to reload Nginx"

# Obtain SSL certificate
log_message "\nObtaining SSL certificate..." "$BLUE"
if [ "$IS_SUBDOMAIN" = true ]; then
    sudo certbot --nginx -d $DOMAIN --non-interactive --agree-tos --email admin@${DOMAIN#*.}
else
    sudo certbot --nginx -d $DOMAIN -d www.$DOMAIN --non-interactive --agree-tos --email admin@$DOMAIN
fi
check_status "Failed to obtain SSL certificate"

# Configure final Nginx with SSL
log_message "\nConfiguring final Nginx setup..." "$BLUE"
sudo tee /etc/nginx/sites-available/$DOMAIN > /dev/null << EOL
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;

    location / {
        return 301 https://\$host\$request_uri;
    }
}

server {
    listen 443 ssl;
    listen [::]:443 ssl;
    http2 on;

    server_name $DOMAIN;

    ssl_certificate /etc/letsencrypt/live/$DOMAIN/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$DOMAIN/privkey.pem;

    # SSL configuration
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers on;
    ssl_ciphers EECDH+AESGCM:EDH+AESGCM;
    ssl_session_timeout 10m;
    ssl_session_cache shared:SSL:10m;

    # Security headers
    add_header Strict-Transport-Security "max-age=63072000" always;
    add_header X-Frame-Options DENY;
    add_header X-Content-Type-Options nosniff;
    add_header X-XSS-Protection "1; mode=block";

    # Logging
    access_log /var/log/nginx/algomirror_access.log;
    error_log /var/log/nginx/algomirror_error.log;

    # SSE streaming endpoints - MUST be before main location
    location /trading/api/option-chain/stream {
        proxy_pass http://unix:$SOCKET_FILE;
        proxy_http_version 1.1;

        # Critical for SSE streaming
        proxy_buffering off;
        proxy_cache off;
        chunked_transfer_encoding off;

        # SSE requires empty Connection header
        proxy_set_header Connection '';
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;

        # Long timeout for persistent SSE connections
        proxy_read_timeout 86400s;
        proxy_send_timeout 86400s;
    }

    # Main app via Unix socket
    location / {
        proxy_pass http://unix:$SOCKET_FILE;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_redirect off;

        # Timeouts for long-running requests
        proxy_connect_timeout 60s;
        proxy_send_timeout 60s;
        proxy_read_timeout 60s;
    }

    # Static files
    location /static {
        alias $ALGOMIRROR_PATH/app/static;
        expires 30d;
        add_header Cache-Control "public, immutable";
    }
}
EOL

sudo nginx -t
check_status "Failed to validate Nginx configuration"

# Check and handle existing systemd service
handle_existing "/etc/systemd/system/$SERVICE_NAME.service" "systemd service" "AlgoMirror service file"

# Create systemd service
log_message "\nCreating systemd service..." "$BLUE"
sudo tee /etc/systemd/system/$SERVICE_NAME.service > /dev/null << EOL
[Unit]
Description=AlgoMirror Multi-Account Management Platform
After=network.target

[Service]
User=www-data
Group=www-data
WorkingDirectory=/var/python/algomirror/app
ExecStart=/bin/bash -c 'source /var/python/algomirror/venv/bin/activate && exec gunicorn \
    --worker-class eventlet \
    --timeout 0 \
    -w 1 \
    --bind unix:/var/python/algomirror/algomirror.sock \
    --log-level info \
    --access-logfile /var/python/algomirror/logs/access.log \
    --error-logfile /var/python/algomirror/logs/error.log \
    wsgi:app'
Restart=always
RestartSec=5
TimeoutSec=60

[Install]
WantedBy=multi-user.target
EOL
check_status "Failed to create systemd service"

# Reload systemd and start services
log_message "\nStarting services..." "$BLUE"
sudo systemctl daemon-reload
sudo systemctl enable $SERVICE_NAME
sudo systemctl start $SERVICE_NAME
sudo systemctl restart nginx
check_status "Failed to start services"

# Verify service is running
log_message "\nVerifying service status..." "$BLUE"
sleep 3
if sudo systemctl is-active --quiet $SERVICE_NAME; then
    log_message "AlgoMirror service is running" "$GREEN"
else
    log_message "Warning: AlgoMirror service may not be running properly" "$YELLOW"
    log_message "Check logs: sudo journalctl -u $SERVICE_NAME -n 50" "$BLUE"
fi

# Installation complete
log_message "\n╔════════════════════════════════════════════════════════╗" "$GREEN"
log_message "║       ALGOMIRROR INSTALLATION COMPLETED!               ║" "$GREEN"
log_message "╚════════════════════════════════════════════════════════╝" "$GREEN"

log_message "\n=== Installation Summary ===" "$YELLOW"
log_message "Domain: https://$DOMAIN" "$BLUE"
log_message "Installation Directory: $ALGOMIRROR_PATH" "$BLUE"
log_message "Database: $BASE_PATH/instance/algomirror.db" "$BLUE"
log_message "Environment File: $ENV_FILE" "$BLUE"
log_message "Socket File: $SOCKET_FILE" "$BLUE"
log_message "Service Name: $SERVICE_NAME" "$BLUE"
log_message "Nginx Config: /etc/nginx/sites-available/$DOMAIN" "$BLUE"
log_message "SSL: Enabled with Let's Encrypt" "$BLUE"
log_message "Installation Log: $LOG_FILE" "$BLUE"

log_message "\n=== Important Notes ===" "$YELLOW"
log_message "1. AlgoMirror is a SINGLE-USER application" "$GREEN"
log_message "2. The FIRST user to register will become the admin" "$GREEN"
log_message "3. After first registration, ALL future registrations are blocked" "$GREEN"
log_message "4. Visit https://$DOMAIN and register immediately" "$GREEN"

log_message "\n=== Next Steps ===" "$YELLOW"
log_message "1. Visit https://$DOMAIN" "$GREEN"
log_message "2. Register your admin account (FIRST USER ONLY!)" "$GREEN"
log_message "3. Add your OpenAlgo account connections" "$GREEN"
log_message "4. Configure broker settings" "$GREEN"

log_message "\n=== Useful Commands ===" "$YELLOW"
log_message "Check Status: sudo systemctl status $SERVICE_NAME" "$BLUE"
log_message "View Logs: sudo journalctl -u $SERVICE_NAME -f" "$BLUE"
log_message "Restart Service: sudo systemctl restart $SERVICE_NAME" "$BLUE"
log_message "View Application Logs: tail -f $ALGOMIRROR_PATH/logs/algomirror.log" "$BLUE"
log_message "View Error Logs: tail -f $BASE_PATH/logs/error.log" "$BLUE"

log_message "\n=== Troubleshooting ===" "$YELLOW"
log_message "If login fails with 'Internal Server Error':" "$BLUE"
log_message "  1. Check application logs: tail -f $ALGOMIRROR_PATH/logs/algomirror.log" "$BLUE"
log_message "  2. Verify flask_session directory exists: ls -la $ALGOMIRROR_PATH/flask_session" "$BLUE"
log_message "  3. Check permissions: sudo chown -R www-data:www-data $BASE_PATH" "$BLUE"
log_message "  4. Restart service: sudo systemctl restart $SERVICE_NAME" "$BLUE"

log_message "\n=== Security Reminder ===" "$YELLOW"
log_message "Your encryption key is: $ENCRYPTION_KEY" "$RED"
log_message "Keep this key safe! It's used to encrypt API keys in the database." "$RED"
log_message "It's stored in $ENV_FILE" "$RED"

log_message "\n✅ Installation complete! Access your AlgoMirror at: https://$DOMAIN" "$GREEN"
