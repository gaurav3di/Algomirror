from flask_wtf import FlaskForm
from wtforms import StringField, SelectField, SubmitField, BooleanField
from wtforms.validators import DataRequired, URL, Length, ValidationError
from app.models import TradingAccount
from flask_login import current_user

class AddAccountForm(FlaskForm):
    account_name = StringField('Account Name', validators=[
        DataRequired(),
        Length(min=3, max=100, message='Account name must be between 3 and 100 characters.')
    ])
    broker_name = SelectField('Broker', choices=[
        ('5paisa', '5paisa'),
        ('5paisa (XTS)', '5paisa (XTS)'),
        ('Aliceblue', 'Aliceblue'),
        ('AngelOne', 'AngelOne'),
        ('Compositedge (XTS)', 'Compositedge (XTS)'),
        ('Definedge', 'Definedge'),
        ('Dhan', 'Dhan'),
        ('Firstock', 'Firstock'),
        ('Flattrade', 'Flattrade'),
        ('Fyers', 'Fyers'),
        ('Groww', 'Groww'),
        ('IIFL (XTS)', 'IIFL (XTS)'),
        ('IndiaBulls', 'IndiaBulls'),
        ('IndMoney', 'IndMoney'),
        ('Kotak Securities', 'Kotak Securities'),
        ('Motilal Oswal', 'Motilal Oswal'),
        ('Paytm', 'Paytm'),
        ('Pocketful', 'Pocketful'),
        ('Shoonya', 'Shoonya'),
        ('Tradejini', 'Tradejini'),
        ('Upstox', 'Upstox'),
        ('Wisdom Capital (XTS)', 'Wisdom Capital (XTS)'),
        ('Zebu', 'Zebu'),
        ('Zerodha', 'Zerodha')
    ], validators=[DataRequired()])

    host_url = StringField('OpenAlgo Host URL', validators=[
        DataRequired(),
        URL(message='Please enter a valid URL.')
    ], default='http://127.0.0.1:5000')
    
    websocket_url = StringField('WebSocket URL', validators=[
        DataRequired(),
        Length(max=500, message='WebSocket URL is too long.')
    ], default='ws://127.0.0.1:8765')
    
    api_key = StringField('OpenAlgo API Key', validators=[
        DataRequired(),
        Length(min=10, message='API Key seems too short.')
    ])
    
    is_primary = BooleanField('Set as Primary Account')
    
    submit = SubmitField('Add Account')
    
    def validate_account_name(self, account_name):
        account = TradingAccount.query.filter_by(
            user_id=current_user.id,
            account_name=account_name.data
        ).first()
        if account:
            raise ValidationError('You already have an account with this name.')

class EditAccountForm(FlaskForm):
    account_name = StringField('Account Name', validators=[
        DataRequired(),
        Length(min=3, max=100, message='Account name must be between 3 and 100 characters.')
    ])
    broker_name = SelectField('Broker', choices=[
        ('5paisa', '5paisa'),
        ('5paisa (XTS)', '5paisa (XTS)'),
        ('Aliceblue', 'Aliceblue'),
        ('AngelOne', 'AngelOne'),
        ('Compositedge (XTS)', 'Compositedge (XTS)'),
        ('Definedge', 'Definedge'),
        ('Dhan', 'Dhan'),
        ('Firstock', 'Firstock'),
        ('Flattrade', 'Flattrade'),
        ('Fyers', 'Fyers'),
        ('Groww', 'Groww'),
        ('IIFL (XTS)', 'IIFL (XTS)'),
        ('IndiaBulls', 'IndiaBulls'),
        ('IndMoney', 'IndMoney'),
        ('Kotak Securities', 'Kotak Securities'),
        ('Motilal Oswal', 'Motilal Oswal'),
        ('Paytm', 'Paytm'),
        ('Pocketful', 'Pocketful'),
        ('Shoonya', 'Shoonya'),
        ('Tradejini', 'Tradejini'),
        ('Upstox', 'Upstox'),
        ('Wisdom Capital (XTS)', 'Wisdom Capital (XTS)'),
        ('Zebu', 'Zebu'),
        ('Zerodha', 'Zerodha')
    ], validators=[DataRequired()])

    host_url = StringField('OpenAlgo Host URL', validators=[
        DataRequired(),
        URL(message='Please enter a valid URL.')
    ])
    
    websocket_url = StringField('WebSocket URL', validators=[
        DataRequired(),
        Length(max=500, message='WebSocket URL is too long.')
    ])
    
    api_key = StringField('OpenAlgo API Key', validators=[
        Length(min=10, message='API Key seems too short.')
    ])
    
    is_primary = BooleanField('Set as Primary Account')
    is_active = BooleanField('Account Active')
    
    update = SubmitField('Update Account')
    
    def __init__(self, original_name, *args, **kwargs):
        super(EditAccountForm, self).__init__(*args, **kwargs)
        self.original_name = original_name
    
    def validate_account_name(self, account_name):
        if account_name.data != self.original_name:
            account = TradingAccount.query.filter_by(
                user_id=current_user.id,
                account_name=account_name.data
            ).first()
            if account:
                raise ValidationError('You already have an account with this name.')