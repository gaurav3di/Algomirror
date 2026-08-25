from flask import Blueprint

equity_bp = Blueprint('equity', __name__, url_prefix='/equity')

from app.equity import routes
