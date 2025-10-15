from flask import render_template, redirect, url_for, current_app
from flask_login import login_required, current_user
from app.main import main_bp
from app.models import TradingAccount, ActivityLog, User
from openalgo import api
from datetime import datetime
from sqlalchemy import desc

@main_bp.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('main.dashboard'))

    # Check if registration is available (single-user app - only if no users exist)
    registration_available = (User.query.count() == 0)

    return render_template('main/index.html', registration_available=registration_available)

@main_bp.route('/dashboard')
@login_required
def dashboard():
    # Check if user has any accounts
    accounts = current_user.get_active_accounts()
    
    if not accounts:
        return redirect(url_for('accounts.add'))
    
    # Aggregate data from all accounts
    dashboard_data = {
        'total_accounts': len(accounts),
        'connected_accounts': 0,
        'total_funds': 0.0,
        'total_pnl': 0.0,
        'total_positions': 0,
        'total_holdings': 0,
        'accounts_summary': []
    }
    
    for account in accounts:
        account_summary = {
            'id': account.id,
            'name': account.account_name,
            'broker': account.broker_name,
            'status': account.connection_status,
            'last_update': account.last_data_update,
            'funds': 0.0,
            'pnl': 0.0,
            'positions': 0,
            'holdings': 0
        }
        
        if account.connection_status == 'connected':
            dashboard_data['connected_accounts'] += 1
        
        # Process funds data
        if account.last_funds_data:
            try:
                available_cash = float(account.last_funds_data.get('availablecash', 0))
                m2m_realized = float(account.last_funds_data.get('m2mrealized', 0))
                m2m_unrealized = float(account.last_funds_data.get('m2munrealized', 0))
                
                account_summary['funds'] = available_cash
                account_summary['pnl'] = m2m_realized + m2m_unrealized
                
                dashboard_data['total_funds'] += available_cash
                dashboard_data['total_pnl'] += m2m_realized + m2m_unrealized
            except (ValueError, TypeError):
                pass
        
        # Process positions data
        if account.last_positions_data:
            try:
                positions = account.last_positions_data if isinstance(account.last_positions_data, list) else []
                account_summary['positions'] = len([p for p in positions if float(p.get('quantity', 0)) != 0])
                dashboard_data['total_positions'] += account_summary['positions']
            except (ValueError, TypeError):
                pass
        
        # Process holdings data
        if account.last_holdings_data:
            try:
                holdings_data = account.last_holdings_data
                if isinstance(holdings_data, dict) and 'holdings' in holdings_data:
                    holdings = holdings_data['holdings']
                    account_summary['holdings'] = len(holdings) if isinstance(holdings, list) else 0
                else:
                    account_summary['holdings'] = len(holdings_data) if isinstance(holdings_data, list) else 0
                
                dashboard_data['total_holdings'] += account_summary['holdings']
            except (ValueError, TypeError):
                pass
        
        dashboard_data['accounts_summary'].append(account_summary)
    
    # Recent activity (last 10 activities)
    recent_activities = current_user.logs.order_by(
        desc(ActivityLog.created_at)
    ).limit(10).all()
    
    current_app.logger.info(
        f'Dashboard accessed by user {current_user.username}',
        extra={
            'event': 'dashboard_access',
            'user_id': current_user.id,
            'accounts_count': len(accounts)
        }
    )
    
    return render_template('main/dashboard.html', 
                         dashboard_data=dashboard_data,
                         recent_activities=recent_activities)