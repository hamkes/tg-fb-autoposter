"""Shared Flask extensions (SQLAlchemy ORM + Flask-Login).

Kept in their own module so models.py and app.py can both import them without
circular imports.
"""
from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = "login_page"
login_manager.login_message = "Please sign in to access this workspace."