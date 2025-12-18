#!/usr/bin/env python3
"""
Database initialization script for AlgoMirror
"""

import os
import sys
from flask import Flask
from app import create_app, db
from app.models import User, TradingAccount, ActivityLog, TradingHoursTemplate, TradingSession, MarketHoliday

# Ensure the instance directory exists (required for SQLite DB path)
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
INSTANCE_DIR = os.path.join(PROJECT_ROOT, 'instance')
os.makedirs(INSTANCE_DIR, exist_ok=True)

def init_database():
    """Initialize the database with tables"""
    print("Initializing AlgoMirror database...")
    
    # Create Flask app
    app = create_app('development')
    
    with app.app_context():
        try:
            # Create all tables
            db.create_all()
            print("[SUCCESS] Database tables created successfully!")
            
            # Print database info
            users_count = User.query.count()
            accounts_count = TradingAccount.query.count()
            logs_count = ActivityLog.query.count()
            
            # Initialize trading hours defaults
            from app.utils.init_trading_hours import init_trading_hours_defaults
            try:
                init_trading_hours_defaults()
                templates_count = TradingHoursTemplate.query.count()
                holidays_count = MarketHoliday.query.count()
            except Exception as e:
                print(f"[WARNING] Could not initialize trading hours defaults: {e}")
                templates_count = 0
                holidays_count = 0
            
            print(f"\n[INFO] Database Statistics:")
            print(f"   Users: {users_count}")
            print(f"   Trading Accounts: {accounts_count}")
            print(f"   Activity Logs: {logs_count}")
            print(f"   Trading Templates: {templates_count}")
            print(f"   Market Holidays: {holidays_count}")
            
            if users_count == 0:
                print("\n[INFO] No users found in database.")
                print("   The first user to register will automatically become an admin.")
                print("   Please go to http://localhost:8000 and register your first account.")
            else:
                admin_count = User.query.filter_by(is_admin=True).count()
                print(f"\n[INFO] Total Users: {users_count} (Admins: {admin_count})")
            
            print(f"\n[INFO] Database file: {os.path.abspath('algomirror.db')}")
            print("[SUCCESS] Database initialization completed successfully!")
            
        except Exception as e:
            print(f"[ERROR] Error initializing database: {str(e)}")
            return False
    
    return True

def reset_database():
    """Reset the database (drop all tables and recreate)"""
    print("[WARNING] Resetting AlgoMirror database...")
    response = input("This will delete all data. Are you sure? (yes/no): ")
    
    if response.lower() != 'yes':
        print("[CANCELLED] Database reset cancelled.")
        return False
    
    app = create_app('development')
    
    with app.app_context():
        try:
            # Drop all tables
            db.drop_all()
            print("[INFO] All tables dropped")
            
            # Recreate tables
            db.create_all()
            print("[SUCCESS] Database tables recreated")
            
            # Initialize trading hours defaults
            from app.utils.init_trading_hours import init_trading_hours_defaults
            try:
                init_trading_hours_defaults()
                print("[SUCCESS] Trading hours defaults initialized")
            except Exception as e:
                print(f"[WARNING] Could not initialize trading hours defaults: {e}")
            
            print("\n[INFO] No default users created.")
            print("   The first user to register will automatically become an admin.")
            print("   Please go to http://localhost:8000 and register your first account.")
            
            print("\n[SUCCESS] Database reset completed successfully!")
            
        except Exception as e:
            print(f"[ERROR] Error resetting database: {str(e)}")
            return False
    
    return True

def create_test_data():
    """Create some test data for development"""
    print("Creating test data...")
    
    app = create_app('development')
    
    with app.app_context():
        try:
            # Check if this will be the first user
            user_count = User.query.count()
            
            # Create a test admin user (if no users exist)
            if user_count == 0:
                admin_user = User(
                    username='admin',
                    email='admin@algomirror.local',
                    is_admin=True  # First user is admin
                )
                admin_user.set_password('Admin@123')
                db.session.add(admin_user)
                db.session.commit()
                print("[SUCCESS] Test admin user created (username: admin, password: Admin@123)")
            
            # Create a test regular user
            test_user = User.query.filter_by(username='testuser').first()
            if not test_user:
                test_user = User(
                    username='testuser',
                    email='test@algomirror.local',
                    is_admin=False  # Regular user
                )
                test_user.set_password('Test@123')
                db.session.add(test_user)
                db.session.commit()
                print("[SUCCESS] Test user created (username: testuser, password: Test@123)")
            
            print("[SUCCESS] Test data created successfully!")
            
        except Exception as e:
            print(f"[ERROR] Error creating test data: {str(e)}")
            return False
    
    return True

def main():
    """Main function"""
    if len(sys.argv) > 1:
        command = sys.argv[1].lower()
        
        if command == 'reset':
            reset_database()
        elif command == 'testdata':
            create_test_data()
        elif command == 'init':
            init_database()
        else:
            print("Usage: python init_db.py [init|reset|testdata]")
            print("  init     - Initialize database (default)")
            print("  reset    - Reset database (WARNING: deletes all data)")
            print("  testdata - Create test data")
    else:
        init_database()

if __name__ == '__main__':
    main()
