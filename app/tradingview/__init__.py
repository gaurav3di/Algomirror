"""
TradingView Blueprint
Provides spread monitoring with TradingView charts
"""
from flask import Blueprint

tradingview_bp = Blueprint('tradingview', __name__, url_prefix='/tradingview')

from app.tradingview import routes
