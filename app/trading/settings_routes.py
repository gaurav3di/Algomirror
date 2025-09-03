from flask import Blueprint, render_template, request, jsonify, flash, redirect, url_for
from flask_login import login_required, current_user
from app import db
from app.models import TradingSettings
from app.utils.rate_limiter import auth_rate_limit

settings_bp = Blueprint('settings', __name__, url_prefix='/trading/settings')

@settings_bp.route('/')
@login_required
@auth_rate_limit()
def index():
    """Display trading settings page"""
    # Get or create default settings for the user
    TradingSettings.get_or_create_defaults(current_user.id)
    
    # Fetch user's settings
    nifty_settings = TradingSettings.query.filter_by(
        user_id=current_user.id,
        symbol='NIFTY'
    ).first()
    
    banknifty_settings = TradingSettings.query.filter_by(
        user_id=current_user.id,
        symbol='BANKNIFTY'
    ).first()
    
    sensex_settings = TradingSettings.query.filter_by(
        user_id=current_user.id,
        symbol='SENSEX'
    ).first()
    
    return render_template('trading/settings.html',
                         nifty_settings=nifty_settings,
                         banknifty_settings=banknifty_settings,
                         sensex_settings=sensex_settings)

@settings_bp.route('/update', methods=['POST'])
@login_required
@auth_rate_limit()
def update():
    """Update trading settings"""
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({'success': False, 'message': 'No data provided'}), 400
        
        symbol = data.get('symbol')
        if symbol not in ['NIFTY', 'BANKNIFTY', 'SENSEX']:
            return jsonify({'success': False, 'message': 'Invalid symbol'}), 400
        
        # Validate inputs
        lot_size = data.get('lot_size')
        freeze_quantity = data.get('freeze_quantity')
        
        if not lot_size or not freeze_quantity:
            return jsonify({'success': False, 'message': 'Lot size and freeze quantity are required'}), 400
        
        try:
            lot_size = int(lot_size)
            freeze_quantity = int(freeze_quantity)
        except ValueError:
            return jsonify({'success': False, 'message': 'Invalid number format'}), 400
        
        # Validate ranges
        if lot_size <= 0 or lot_size > 1000:
            return jsonify({'success': False, 'message': 'Lot size must be between 1 and 1000'}), 400
        
        if freeze_quantity <= 0 or freeze_quantity > 50000:
            return jsonify({'success': False, 'message': 'Freeze quantity must be between 1 and 50000'}), 400
        
        # Calculate max lots per order
        max_lots = freeze_quantity // lot_size
        if max_lots == 0:
            max_lots = 1
        
        # Get or create setting
        setting = TradingSettings.query.filter_by(
            user_id=current_user.id,
            symbol=symbol
        ).first()
        
        if not setting:
            setting = TradingSettings(
                user_id=current_user.id,
                symbol=symbol
            )
            db.session.add(setting)
        
        # Update values
        setting.lot_size = lot_size
        setting.freeze_quantity = freeze_quantity
        setting.max_lots_per_order = max_lots
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': f'{symbol} settings updated successfully',
            'data': {
                'symbol': symbol,
                'lot_size': lot_size,
                'freeze_quantity': freeze_quantity,
                'max_lots_per_order': max_lots
            }
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500

@settings_bp.route('/get/<symbol>')
@login_required
@auth_rate_limit()
def get_setting(symbol):
    """Get trading settings for a specific symbol"""
    if symbol not in ['NIFTY', 'BANKNIFTY', 'SENSEX']:
        return jsonify({'success': False, 'message': 'Invalid symbol'}), 400
    
    setting = TradingSettings.query.filter_by(
        user_id=current_user.id,
        symbol=symbol
    ).first()
    
    if not setting:
        # Create default if doesn't exist
        TradingSettings.get_or_create_defaults(current_user.id)
        setting = TradingSettings.query.filter_by(
            user_id=current_user.id,
            symbol=symbol
        ).first()
    
    return jsonify({
        'success': True,
        'data': {
            'symbol': setting.symbol,
            'lot_size': setting.lot_size,
            'freeze_quantity': setting.freeze_quantity,
            'max_lots_per_order': setting.max_lots_per_order
        }
    })

@settings_bp.route('/reset', methods=['POST'])
@login_required
@auth_rate_limit()
def reset():
    """Reset settings to defaults"""
    try:
        # Delete existing settings
        TradingSettings.query.filter_by(user_id=current_user.id).delete()
        
        # Create defaults
        TradingSettings.get_or_create_defaults(current_user.id)
        
        flash('Settings reset to defaults successfully', 'success')
        return jsonify({'success': True, 'message': 'Settings reset to defaults'})
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500